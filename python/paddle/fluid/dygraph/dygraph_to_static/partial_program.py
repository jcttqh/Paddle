# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import print_function
import numpy as np
import six

import paddle
from paddle.fluid import framework, backward, core
from paddle.fluid.dygraph import layers
from paddle.fluid.dygraph.base import switch_to_static_graph
from paddle.fluid.dygraph.dygraph_to_static import logging_utils
from paddle.fluid.dygraph.dygraph_to_static.return_transformer import RETURN_NO_VALUE_MAGIC_NUM
from paddle.fluid.layers.utils import flatten
from paddle.fluid.layers.utils import pack_sequence_as
import paddle.compat as cpt


class NestSequence(object):
    """
    A wrapper class that easily to flatten and restore the nest structure of
    given sequence.
    """

    def __init__(self, raw_input, need_check=False):
        self.__raw_input = raw_input
        self.__var_ids = self._get_var_ids()
        self._check_non_variable(need_check)

    def tolist(self):
        """
        Flattens the nested sequences into single list.
        """
        return flatten(self.__raw_input)

    def restore(self, value_list):
        """
        Restores the nested sequence from value list.
        """
        assert len(self.tolist()) == len(value_list)
        return pack_sequence_as(self.__raw_input, value_list)

    def _get_var_ids(self):
        var_ids = []
        for idx, var in enumerate(self.tolist()):
            if isinstance(var, (framework.Variable, core.VarBase)):
                var_ids.append(idx)

        return var_ids

    def _check_non_variable(self, need_check):
        """
        Raises warning if output of traced function contains non-tensor type values.
        """
        if need_check:
            warning_types = set()
            for var in self.tolist():
                if not isinstance(var, (framework.Variable, core.VarBase)):
                    warning_types.add(type(var))
            if warning_types:
                logging_utils.warn(
                    "Output of traced function contains non-tensor type values: {}. "
                    "Currently, We don't support to update them while training and will return "
                    "what we first saw. Please try to return them as tensor.".
                    format(list(warning_types)))

    @property
    def var_ids(self):
        return self.__var_ids

    def __getitem__(self, item):
        return self.tolist()[item]


class LazyInitialized(object):
    """
    Descriptor to implement lazy initialization of property.
    """

    def __init__(self, function):
        self.function = function

    def __get__(self, instance, cls):
        val = self.function(instance)
        setattr(instance, self.function.__name__, val)
        return val


def _change_is_test_status(program, is_test):
    # change all `is_test` attributes
    for block in program.blocks:
        for op in block.ops:
            if op.has_attr('is_test'):
                op._set_attr('is_test', is_test)
    return program


class PartialProgramLayer(layers.Layer):
    """
    PartialProgramLayer wraps all the ops from layers decorated by `@declarative`
    and execute them as a static subgraph.

    .. note::
        **1. This is a very low level API. Users should not use this API
             directly. Please use `partial_program_from(concrete_program)`
             to create it.
        **2. LoDTensorArray is not currently supported in the output.

    Args:
        main_program(Program): The main program that contains ops need to be executed.
        inputs(list[Variable]): The input list of the decorated function by `@declarative`.
        outputs(list[Variable]): The output list of the decorated function by `@declarative`.
        parameters(list[VarBase]|None): All trainable parameters included in the program. Default None.

    Returns:
        Layer: A Layer object that run all ops internally in static mode.
    """

    def __init__(self, main_program, inputs, outputs, parameters=None):
        super(PartialProgramLayer, self).__init__()
        self._inputs = NestSequence(inputs)
        self._outputs = NestSequence(outputs, need_check=True)
        self._params = parameters if parameters is not None else []

        self._origin_main_program = self._verify_program(main_program)
        self._inner_scope = core.Scope()
        # Set default mode to train
        self._double_grads = self._get_double_grads(self._origin_main_program)
        self.training = True

    @LazyInitialized
    def _infer_program(self):
        """
        Lazy initialized property of infer_program.
        """
        return self._clone_for_test(self._origin_main_program)

    @LazyInitialized
    def _train_program(self):
        """
        Lazy initialized property of train_program.
        """
        train_program = self._append_backward_desc(self._origin_main_program)
        # Note: Only set grad type once after initializing train program. So we
        # put it here.
        self._set_grad_type(self._params, train_program)

        return train_program

    def _verify_program(self, main_program):
        """
        Verify that the program parameter is initialized, prune some unused params,
        and remove redundant op callstack.
        """
        # 1. Check all params from main program can be found in self._params
        self._check_params_all_inited(main_program)
        # 2. Prune the parameters not used anywhere in the program.
        self._prune_unused_params(main_program)

        return main_program

    @switch_to_static_graph
    def _append_backward_desc(self, main_program):
        # make sure all status of is_test are False in train mode.
        program = _change_is_test_status(main_program.clone(), is_test=False)
        targets = []
        for out in self._outputs.tolist():
            if isinstance(out, framework.Variable):
                targets.append(program.global_block().var(out.name))

        if targets and self._params:
            backward.gradients(targets=targets, inputs=[])

        return program

    def _prune_unused_params(self, program):
        """
        Prune the parameters not used anywhere in the program.
        The `@declarative` may only decorated a sub function which
        contains some unused parameters created in `__init__`.
        So prune these parameters to avoid unnecessary operations in
        `run_program_op`.
        """
        required_params = []
        for param in self._params:
            found_param = False
            for block in program.blocks:
                for op in block.ops:
                    if param.name in op.input_arg_names or param.name in op.output_arg_names:
                        required_params.append(param)
                        found_param = True
                        break
                if found_param:
                    break

        self._params = required_params

    def _get_double_grads(self, program):
        double_grads = []
        for block in program.blocks:
            for name in block.vars:
                if "@GRAD" in name:
                    var_desc = block.vars[name].desc
                    var_base = core.VarBase(var_desc.dtype(),
                                            var_desc.shape(),
                                            var_desc.name(),
                                            var_desc.type(), False)
                    double_grads.append(var_base)
        return double_grads

    def forward(self, inputs):
        in_vars, out_vars, tmp_scope_vec = self._prepare(inputs)
        framework._dygraph_tracer().trace_op(
            type='run_program',
            inputs={
                'X': valid_vars(in_vars),
                'Params': valid_vars(self._params)
            },
            outputs={
                'Out': valid_vars(out_vars),
                'OutScope': tmp_scope_vec,
                'DOut': valid_vars(self._double_grads)
            },
            attrs={
                'global_block': self.program.desc.block(0),
                'start_op_index': 0,
                'end_op_index': self._infer_program.desc.block(0).op_size(),
                'is_test': not self.training
            })

        restored_nest_out = self._restore_out(out_vars)
        return self._remove_no_value(restored_nest_out)

    @property
    def program(self):
        return self._train_program if self.training else self._infer_program

    def _prepare(self, inputs):
        """
        Prepare inputs, outputs, attrs.
        """
        assert isinstance(inputs, (tuple, list))
        # Flatten inputs with nested structure into single list.
        flatten_inputs = flatten(inputs)
        # Convert variable into VarBase and feed in training data.
        input_vars = []
        for i, value in enumerate(flatten_inputs):
            if isinstance(value, np.ndarray):
                var = core.VarBase(
                    value=value,
                    name=self._inputs[i].desc.name(),
                    persistable=False,
                    place=framework._current_expected_place(),
                    zero_copy=True)
            elif isinstance(value, core.VarBase):
                if value.stop_gradient:
                    # NOTE(Aurelius84): If var is on CPUPlace, it will be transformed multi times
                    # into CUDAPlace when it's as input of multi Ops. so we move it in advance
                    # to avoid this problem.
                    var = paddle.to_tensor(
                        value,
                        dtype=value.dtype,
                        place=framework._current_expected_place(),
                        stop_gradient=True)
                else:
                    var = value
                var.name = self._inputs[i].desc.name()
            else:
                continue
            input_vars.append(var)

        # Create VarBase to receive output data.
        out_vars = []
        for idx in self._outputs.var_ids:
            var = self._outputs[idx]
            assert isinstance(var, framework.Variable)
            var_desc = var.desc
            var_base = core.VarBase(var_desc.dtype(),
                                    var_desc.shape(),
                                    var_desc.name(), var_desc.type(), False)
            out_vars.append(var_base)

        # Hold forward variables
        tmp_scope_vec = core.VarBase(core.VarDesc.VarType.FP32, [],
                                     "program_out_scope",
                                     core.VarDesc.VarType.STEP_SCOPES, True)

        tmp_scope_vec.value().set_scope(self._inner_scope)

        return input_vars, out_vars, tmp_scope_vec

    def _restore_out(self, out_vars):
        """
        Restores same nested outputs by only replacing the Variable with VarBase.
        """

        flatten_outputs = self._outputs.tolist()
        for i, idx in enumerate(self._outputs.var_ids):
            flatten_outputs[idx] = out_vars[i]
        outs = self._outputs.restore(flatten_outputs)
        if outs is not None and len(outs) == 1:
            outs = outs[0]

        return outs

    @switch_to_static_graph
    def _clone_for_test(self, main_program):
        return main_program.clone(for_test=True)

    def _is_no_value(self, var):
        if isinstance(var, core.VarBase):
            if var.shape == [1] and var.numpy()[0] == RETURN_NO_VALUE_MAGIC_NUM:
                return True
        return False

    def _remove_no_value(self, out_vars):
        """
        Removes invalid value for various-length return statement
        """
        if isinstance(out_vars, core.VarBase):
            if self._is_no_value(out_vars):
                return None
            return out_vars
        elif isinstance(out_vars, (tuple, list)):
            if isinstance(out_vars, tuple):
                res = tuple(
                    var for var in out_vars if not self._is_no_value(var))
            else:
                # isinstance(out_vars, list)
                res = [var for var in out_vars if not self._is_no_value(var)]

            has_removed = (len(out_vars) > len(res))
            # len(out_vars) > len(res) means we have removed var. This is
            # preventing out_vars is empty or just one element at the beginning
            if len(res) == 0 and has_removed:
                return None
            elif len(res) == 1 and has_removed:
                return res[0]
            return res

        return out_vars

    def _set_grad_type(self, params, train_program):
        # NOTE: if user set sparse gradient mode, the param's gradient
        # will be SelectedRows, not LoDTensor. But tracer will just
        # set param grad VarBase by forward VarBase(LoDTensor)
        # If we don't change grad_var type here, RunProgramOp need
        # transform SelectedRows to LoDTensor forcibly, it may not
        # be user wanted result.
        for param in params:
            grad_name = param.name + core.grad_var_suffix()
            grad_var = train_program.desc.block(0).find_var(
                cpt.to_bytes(grad_name))
            # NOTE: cannot find var desc maybe no problem, such as in batch_norm
            if grad_var is None:
                continue
            param._set_grad_type(grad_var.type())

    def _remove_op_call_stack(self, main_program):
        """
        Remove op's python call stack with redundant low-level error messages related to
        transforamtions to avoid confusing users.
        """
        assert isinstance(main_program, framework.Program)
        for block in main_program.blocks:
            for op in block.ops:
                if op.has_attr("op_callstack"):
                    op._remove_attr("op_callstack")

        return main_program

    def _check_params_all_inited(self, main_program):
        """
        Check all params from main program are already initialized, see details as follows:
            1. all parameters in self._params should be type `framework.ParamBase` which are created in dygraph.
            2. all parameters from transformed program can be found in self._params.
               Because they share same data with ParamBase of original dygraph.
        """
        if not isinstance(self._params, (list, tuple)):
            raise TypeError(
                "Type of self._params in PartialProgramLayer should be list or tuple, but received %s."
                % type(self._params))

        param_and_buffer_names_set = set()
        for i, var in enumerate(self._params):
            # self._params constains parameters and buffers with persistable=True.
            if not isinstance(var, core.VarBase):
                raise TypeError(
                    'Type of self._params[{}] in PartialProgramLayer should be Parameter or Variable, but received {}.'.
                    format(i, type(var)))
            param_and_buffer_names_set.add(var.name)

        for block in main_program.blocks:
            for name, var in six.iteritems(block.vars):
                if isinstance(var, framework.Parameter):
                    if name not in param_and_buffer_names_set:
                        raise ValueError(
                            "\n\tWe don't support to define layer with parameters in the function "
                            "decorated by `@declarative`.\n\tBecause that will re-defined parameters "
                            "every time when you run the function.\n\t"
                            "But we found parameter(%s) was created in the decorated function.\n\t"
                            "Please define the layer with parameters in `__init__` function."
                            % name)


def valid_vars(vars):
    """
    Note: run_program_op.InferShape requires `X`/'Out' not be null.
    But it's common in dy2static, fake varBase is created to handle the
    problem.
    """
    if vars:
        return vars
    return [
        core.VarBase(
            value=[1],
            name='Fake_var',
            place=framework._current_expected_place())
    ]


def partial_program_from(concrete_program):
    inputs = concrete_program.inputs
    if inputs and isinstance(inputs[0], layers.Layer):
        inputs = inputs[1:]

    return PartialProgramLayer(concrete_program.main_program, inputs,
                               concrete_program.outputs,
                               concrete_program.parameters)
