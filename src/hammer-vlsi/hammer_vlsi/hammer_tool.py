#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  hammer_tool.py
#  HammerTool - the main Hammer tool abstraction class.
#
#  See LICENSE for licence details.

import inspect
import os
import re
import shlex
from abc import ABCMeta, abstractmethod
from functools import reduce
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple, cast

import hammer_config
import hammer_tech
from hammer_logging import HammerVLSILoggingContext
from hammer_tech import LibraryFilter, Stackup, RoutingDirection, Metal
from hammer_utils import (add_lists, assert_function_type, get_or_else,
                          optional_map)

from .constraints import *
from .hammer_vlsi_impl import HammerToolPauseException, HierarchicalMode
from .hooks import (HammerStepFunction, HammerToolHookAction, HammerToolStep,
                    HookLocation)
from .submit_command import HammerSubmitCommand
from .units import TemperatureValue, TimeValue, VoltageValue

__all__ = ['HammerTool']


def make_raw_hammer_tool_step(func: HammerStepFunction, name: str) -> HammerToolStep:
    # Check the type of the HammerStepFunction
    check_hammer_step_function(func)
    return HammerToolStep(func, name)


def check_hammer_step_function(func: HammerStepFunction) -> None:
    """Internal alias for checking HammerStepFunction signatures."""
    assert_function_type(func, args=[HammerTool], return_type=bool)


class HammerTool(metaclass=ABCMeta):
    # Interface methods.
    @property
    def env_vars(self) -> Dict[str, str]:
        """
        Get the list of environment variables required for this tool.
        Note to subclasses: remember to include variables from super().env_vars!

        :return: Mapping of environment variable -> contents of said variable.
        """
        return {}

    def export_config_outputs(self) -> Dict[str, Any]:
        """
        Export the outputs of this tool to a config.
        By default, this just adds a flag to indicate that the output fragment
        is output-only/not complete.

        Warning: any subclasses must call this method in the base class
        so that all output configs get added correctly.

        :return: Config dictionary of the outputs of this tool.
        """
        return {
            "vlsi.builtins.is_complete": False
        }

    @abstractmethod
    def tool_config_prefix(self) -> str:
        """
        Returns the config prefix that contains all tool specific settings.
        e.g. "synthesis.yosys".

        :return: A string that is the prefix for all tool specific settings.
        """
        pass

    def version(self) -> int:
        """
        Returns the version number of the current tool version, using version_number
        below.

        :return: The version number of the current tool.
        """
        return self.version_number(self.get_setting(self.tool_config_prefix() + ".version"))

    @abstractmethod
    def version_number(self, version: str) -> int:
        """
        Based on the tool, figures out an integer value for the version number.

        :param version: The version number given by the tool vendor.
        :return: An integer representing the version suitable for comparisons.
        """
        pass

    # Setup functions.
    def run(self, hook_actions: List[HammerToolHookAction] = []) -> bool:
        """Run this tool.

        Perform some setup operations to set up the config and tool environment, runs the tool-specific actions defined
        in steps, and collects the outputs.

        :return: True if the tool finished successfully; false otherwise.
        """

        # Ensure that the run_dir exists.
        os.makedirs(self.run_dir, exist_ok=True)

        # Run the list of steps defined for this tool.
        if not self.run_steps(self.steps, hook_actions):
            return False

        # Fill the outputs of the tool.
        return self.fill_outputs()

    @property
    @abstractmethod
    def steps(self) -> List[HammerToolStep]:
        """
        List of steps defined for the execution of this tool.
        """
        pass

    def do_pre_steps(self, first_step: HammerToolStep) -> bool:
        """
        Function to run before the list of steps executes.
        Intended to be overridden by subclasses.

        :param first_step: First step to be taken.
        :return: True if successful, False otherwise.
        """
        return True

    def do_between_steps(self, prev: HammerToolStep, next: HammerToolStep) -> bool:
        """
        Function to run between the execution of two steps.
        Does not include pause hooks.
        Intended to be overridden by subclasses.

        :param prev: The step that just finished
        :param next: The next step about to run.
        :return: True if successful, False otherwise.
        """
        return True

    def do_post_steps(self) -> bool:
        """
        Function to run after the list of steps executes.
        Intended to be overridden by subclasses.

        :return: True if successful, False otherwise.
        """
        return True

    def fill_outputs(self) -> bool:
        """
        Fill the outputs of the tool.
        Note: if you override this, remember to call the superclass method too!

        :return: True if successful, False otherwise.
        """
        return True

    @property
    def _subprocess_env(self) -> dict:
        """
        Internal helper function to set the environment variables for
        self.run_executable().
        """
        env = os.environ.copy()
        # Add HAMMER_DATABASE to the environment for the script.
        env.update({"HAMMER_DATABASE": self.dump_database()})
        env.update(self.env_vars)
        return env

    # Properties.
    @property
    def name(self) -> str:
        """
        Short name of the tool library.
        Typically the folder name (e.g. "dc", "yosys", etc).

        :return: Short name of the tool library.
        """
        try:
            return self._name
        except AttributeError:
            raise ValueError("Internal error: Short name of the tool library not set by hammer-vlsi")

    @name.setter
    def name(self, value: str) -> None:
        """Set the Short name of the tool library."""
        self._name = value # type: str

    @property
    def tool_dir(self) -> str:
        """
        Get the location of the tool library.

        :return: Path to the location of the library.
        """
        try:
            return self._tooldir
        except AttributeError:
            raise ValueError("Internal error: tool dir location not set by hammer-vlsi")

    @tool_dir.setter
    def tool_dir(self, value: str) -> None:
        """Set the directory which contains this tool library."""
        self._tooldir = value # type: str

    @property
    def run_dir(self) -> str:
        """
        Get the location of the run dir, a writable temporary information for use by the tool.
        This should return an absolute path.

        :return: Path to the location of the library.
        """
        try:
            return self._rundir
        except AttributeError:
            raise ValueError("Internal error: run dir location not set by hammer-vlsi")

    @run_dir.setter
    def run_dir(self, path: str) -> None:
        """Set the location of a writable directory which the tool can use to store temporary information."""
        # If the path isn't absolute, make it absolute.
        if not os.path.isabs(path):
            path = os.path.abspath(path)
        self._rundir = path  # type: str

    @property
    def input_files(self) -> Iterable[str]:
        """
        Input files for this tool library.
        The exact nature of the files will depend on the type of library.
        """
        try:
            return self._input_files
        except AttributeError:
            raise ValueError("Nothing set for inputs yet")

    @input_files.setter
    def input_files(self, value: Iterable[str]) -> None:
        """
        Set the input files for this tool library.
        The exact nature of the files will depend on the type of library.
        """
        if not isinstance(value, Iterable):
            raise TypeError("input_files must be a Iterable[str]")
        self._input_files = value # type: Iterable[str]


    @property
    def hierarchical_mode(self) -> HierarchicalMode:
        """
        Input files for this tool library.
        The exact nature of the files will depend on the type of library.
        """
        try:
            return self.attr_getter("_hierarchical_mode", None)
        except AttributeError:
            raise ValueError("HierarchicalMode not set")

    @hierarchical_mode.setter
    def hierarchical_mode(self, value: HierarchicalMode) -> None:
        """
        Set the input files for this tool library.
        The exact nature of the files will depend on the type of library.
        """
        if not isinstance(value, HierarchicalMode):
            raise TypeError("hierarchical_mode must be a HierarchicalMode")
        self.attr_setter("_hierarchical_mode", value)

    @property
    def technology(self) -> hammer_tech.HammerTechnology:
        """
        Get the technology library currently in use.

        :return: HammerTechnology instance
        """
        try:
            return self._technology
        except AttributeError:
            raise ValueError("Internal error: technology not set by hammer-vlsi")

    @technology.setter
    def technology(self, value: hammer_tech.HammerTechnology) -> None:
        """Set the HammerTechnology currently in use."""
        self._technology = value  # type: hammer_tech.HammerTechnology

    @property
    def submit_command(self) -> HammerSubmitCommand:
        """
        Get the submit command used by this tool

        :return HammerSubmitCommand instance
        """
        try:
            return self._submit_command
        except AttributeError:
            raise ValueError("Internal error: technology not set by hammer-vlsi")

    @submit_command.setter
    def submit_command(self, value: HammerSubmitCommand) -> None:
        """
        Set the submit command used by this tool

        :value: HammerSubmitCommand instance
        """
        self._submit_command = value

    @property
    def top_module(self) -> str:
        """
        Get the top-level module.

        :return: The top-level module.
        """
        try:
            return self.attr_getter("_top_module", None)
        except AttributeError:
            raise ValueError("Nothing set for the top-level module yet")

    @top_module.setter
    def top_module(self, value: str) -> None:
        """Set the top-level module."""
        if not (isinstance(value, str)):
            raise TypeError("top_module must be a str")
        self.attr_setter("_top_module", value)

    @property
    def logger(self) -> HammerVLSILoggingContext:
        """Get the logger for this tool."""
        try:
            return self._logger
        except AttributeError:
            raise ValueError("Internal error: logger not set by hammer-vlsi")

    @logger.setter
    def logger(self, value: HammerVLSILoggingContext) -> None:
        """Set the logger for this tool."""
        self._logger = value # type: HammerVLSILoggingContext

    ##############################
    # Implementation helpers for properties
    ##############################
    def attr_getter(self, key: str, default: Any) -> Any:
        """Helper function for implementing the getter of a property with a default.
        If default is None, then raise a AttributeError."""
        if not hasattr(self, key):
            if default is not None:
                setattr(self, key, default)
            else:
                raise AttributeError("No such attribute " + str(key))
        return getattr(self, key)

    def attr_setter(self, key: str, value: Any) -> None:
        """Helper function for implementing the setter of a property with a default."""
        setattr(self, key, value)

    ##############################
    # Hooks
    ##############################
    def check_duplicates(self, lst: List[HammerToolStep]) -> Tuple[bool, Set[str]]:
        """Check that no two steps have the same name."""
        seen_names = set()  # type: Set[str]
        for step in lst:
            if step.name in seen_names:
                self.logger.error("Duplicate step '{step}' encountered".format(step=step.name))
                return False, set()
            else:
                seen_names.add(step.name)
        return True, seen_names

    def run_steps(self, steps: List[HammerToolStep], hook_actions: List[HammerToolHookAction] = []) -> bool:
        """
        Run the given steps, checking for errors/conditions between each step.

        :param steps: List of steps.
        :param hook_actions: List of hook actions.
        :return: Returns true if all the steps are successful.
        """
        duplicate_free, names = self.check_duplicates(steps)
        if not duplicate_free:
            return False

        def has_step(name: str) -> bool:
            return name in names

        # Copy the list of steps
        new_steps = list(steps)

        # Where to resume, if such a hook exists
        resume_step = None  # type: Optional[str]
        # If resume_step is not None, whether to resume pre or post this step
        resume_step_pre = True  # type: bool

        for action in hook_actions:
            if not has_step(action.target_name):
                self.logger.error("Target step '{step}' does not exist".format(step=action.target_name))
                return False

            step_id = -1
            for i in range(len(new_steps)):
                if new_steps[i].name == action.target_name:
                    step_id = i
                    break
            assert step_id != -1

            if action.location == HookLocation.ReplaceStep:
                assert action.step is not None, "ReplaceStep requires a step"
                assert action.target_name == action.step.name, "Replacement step should have the same name"
                new_steps[step_id] = action.step
            elif action.location == HookLocation.InsertPreStep:
                assert action.step is not None, "InsertPreStep requires a step"
                if has_step(action.step.name):
                    self.logger.error("New step '{step}' already exists".format(step=action.step.name))
                    return False
                new_steps.insert(step_id, action.step)
                names.add(action.step.name)
            elif action.location == HookLocation.InsertPostStep:
                assert action.step is not None, "InsertPostStep requires a step"
                if has_step(action.step.name):
                    self.logger.error("New step '{step}' already exists".format(step=action.step.name))
                    return False
                new_steps.insert(step_id + 1, action.step)
                names.add(action.step.name)
            elif action.location == HookLocation.ResumePreStep or action.location == HookLocation.ResumePostStep:
                if resume_step is not None:
                    self.logger.error("More than one resume hook is present")
                    return False
                resume_step = action.target_name
                resume_step_pre = action.location == HookLocation.ResumePreStep
            else:
                assert False, "Should not reach here"

        # Check function types before running
        for step in new_steps:
            if not isinstance(step, HammerToolStep):
                raise ValueError("Element in List[HammerToolStep] is not a HammerToolStep")
            else:
                # Cajole the type checker into accepting that step is a HammerToolStep
                step = cast(HammerToolStep, step)
                check_hammer_step_function(step.func)

        # Run steps.
        prev_step = None  # type: Optional[HammerToolStep]

        for step_index in range(len(new_steps)):
            step = new_steps[step_index]

            self.logger.debug("Running sub-step '{step}'".format(step=step.name))

            # Do this step?
            do_step = True

            if resume_step is not None:
                if resume_step_pre:
                    if resume_step == step.name:
                        self.logger.info("Resuming before '{step}' due to resume hook".format(step=step.name))
                        # Remove resume marker
                        resume_step = None
                    else:
                        self.logger.info("Sub-step '{step}' skipped due to resume hook".format(step=step.name))
                        do_step = False
                else:
                    self.logger.info("Sub-step '{step}' skipped due to resume hook".format(step=step.name))
                    do_step = False

            if do_step:
                try:
                    if prev_step is None:
                        # Run pre-step hook.
                        self.do_pre_steps(step)
                    else:
                        # TODO: find a cleaner way of detecting a pause hook
                        if step.name == "pause":
                            # Don't include "pause" for do_between_steps
                            if step_index + 1 < len(new_steps):
                                self.do_between_steps(prev_step, new_steps[step_index + 1])
                        else:
                            self.do_between_steps(prev_step, step)
                    func_out = step.func(self)  # type: bool
                    prev_step = step
                except HammerToolPauseException:
                    self.logger.info("Sub-step '{step}' paused the tool execution".format(step=step.name))
                    break
                assert isinstance(func_out, bool)
                if not func_out:
                    return False

            if resume_step is not None:
                if not resume_step_pre and resume_step == step.name:
                    self.logger.info("Resuming after '{step}' due to resume hook".format(step=step.name))
                    resume_step = None

        # Run post-steps hook.
        self.do_post_steps()

        return True

    @staticmethod
    def make_step_from_method(func: Callable[[], bool], name: str = "") -> HammerToolStep:
        """
        Create a HammerToolStep from a method.

        :param func: Method for the given substep (e.g. self.elaborate)
        :param name: Name of the hook. If unspecified, defaults to func.__name__.
        :return: A HammerToolStep defining this step.
        """
        if not callable(func):
            raise TypeError("func is not callable")
        if not hasattr(func, "__self__"):
            raise ValueError("This function does not take unbound functions")
        annotations = inspect.getfullargspec(func).annotations
        if annotations != {'return': bool}:
            raise TypeError("Function {func} does not meet the required signature".format(func=str(func)))

        # Wrapper to make __func__ take a proper type annotation for "self"
        def wrapper(x: HammerTool) -> bool:
            return func.__func__(x)  # type: ignore # no type stub for builtin __func__

        if name == "":
            name = func.__name__
        return make_raw_hammer_tool_step(func=wrapper, name=name)

    @staticmethod
    def make_steps_from_methods(funcs: List[Callable[[], bool]]) -> List[HammerToolStep]:
        """
        Create a series of HammerToolStep from the given list of bound methods.

        :param funcs: List of bound methods (e.g. [self.step1, self.step2])
        :return: List of HammerToolSteps
        """
        return list(map(lambda x: HammerTool.make_step_from_method(x), funcs))

    @staticmethod
    def make_step_from_function(func: HammerStepFunction, name: str = "") -> HammerToolStep:
        """
        Create a HammerToolStep from a function.

        :param func: Class function for the given substep
        :param name: Name of the hook. If unspecified, defaults to func.__name__.
        :return: A HammerToolStep defining this step.
        """
        if hasattr(func, "__self__"):
            raise ValueError("This function does not take bound methods")
        if name == "":
            name = func.__name__
        return make_raw_hammer_tool_step(func=func, name=name)

    @staticmethod
    def make_pause_function() -> HammerStepFunction:
        """
        Get a step function which will stop the execution of the tool.
        """
        def pause(x: HammerTool) -> bool:
            raise HammerToolPauseException()
        return pause

    @staticmethod
    def make_replacement_hook(step: str, func: HammerStepFunction) -> HammerToolHookAction:
        """
        Create a hook action which replaces an existing step.

        :return: Hook action which replaces the given step.
        """
        return HammerToolHookAction(
            target_name=step,
            location=HookLocation.ReplaceStep,
            step=HammerTool.make_step_from_function(func, step)
        )

    @staticmethod
    def make_insertion_hook(step: str, location: HookLocation, func: HammerStepFunction) -> HammerToolHookAction:
        """
        Create a hook action is inserted relative to the given step.
        """
        if location != HookLocation.InsertPreStep and location != HookLocation.InsertPostStep:
            raise ValueError("Insertion hook location must be Insert*")

        return HammerToolHookAction(
            target_name=step,
            location=location,
            step=HammerTool.make_step_from_function(func)
        )

    @staticmethod
    def make_resume_hook(step: str, location: HookLocation) -> HammerToolHookAction:
        """
        Create a hook action is inserted relative to the given step.
        """
        if location != HookLocation.ResumePreStep and location != HookLocation.ResumePostStep:
            raise ValueError("Resume hook location must be Resume*")

        return HammerToolHookAction(
            target_name=step,
            location=location,
            step=None
        )

    @staticmethod
    def make_pre_pause_hook(step: str) -> HammerToolHookAction:
        """
        Create pause before the execution of the given step.
        """
        return HammerTool.make_insertion_hook(step, HookLocation.InsertPreStep, HammerTool.make_pause_function())

    @staticmethod
    def make_post_pause_hook(step: str) -> HammerToolHookAction:
        """
        Create pause before the execution of the given step.
        """
        return HammerTool.make_insertion_hook(step, HookLocation.InsertPostStep, HammerTool.make_pause_function())

    @staticmethod
    def make_pre_resume_hook(step: str) -> HammerToolHookAction:
        """
        Resume before the given step.
        Note that only one resume hook may be present.
        """
        return HammerTool.make_resume_hook(step, HookLocation.ResumePreStep)

    @staticmethod
    def make_post_resume_hook(step: str) -> HammerToolHookAction:
        """
        Resume after the given step.
        Note that only one resume hook may be present.
        """
        return HammerTool.make_resume_hook(step, HookLocation.ResumePostStep)

    @staticmethod
    def make_from_to_hooks(from_step: Optional[str] = None,
                           to_step: Optional[str] = None) -> List[HammerToolHookAction]:
        """
        Helper function to create a HammerToolHookAction list which will run from and to the given steps, inclusive.

        :param from_step: Run from the given step, inclusive. Leave as None to resume from the beginning.
        :param to_step: Run to the given step, inclusive. Leave as None to run to the end.
        :return: HammerToolHookAction list for running from and to the given steps, inclusive.
        """
        output = []  # type: List[HammerToolHookAction]
        if from_step is not None:
            output.append(HammerTool.make_pre_resume_hook(from_step))
        if to_step is not None:
            output.append(HammerTool.make_post_pause_hook(to_step))
        return output

    @staticmethod
    def make_pre_insertion_hook(step: str, func: HammerStepFunction) -> HammerToolHookAction:
        """
        Create a hook action is inserted prior to the given step.
        """
        return HammerTool.make_insertion_hook(step, HookLocation.InsertPreStep, func)

    @staticmethod
    def make_post_insertion_hook(step: str, func: HammerStepFunction) -> HammerToolHookAction:
        """
        Create a hook action is inserted after the given step.
        """
        return HammerTool.make_insertion_hook(step, HookLocation.InsertPostStep, func)

    @staticmethod
    def make_removal_hook(step: str) -> HammerToolHookAction:
        """
        Helper function to remove a step by replacing it with an empty step.

        :return: Hook action which replaces the given step.
        """
        def dummy_step(x: HammerTool) -> bool:
            return True
        return HammerToolHookAction(
            target_name=step,
            location=HookLocation.ReplaceStep,
            step=HammerTool.make_step_from_function(dummy_step, step)
        )


    ##############################
    # Accessory functions available to tools.
    # TODO(edwardw): maybe move set_database/get_setting into an interface like UsesHammerDatabase?
    ##############################
    def set_database(self, database: hammer_config.HammerDatabase) -> None:
        """Set the settings database for use by the tool."""
        self._database = database # type: hammer_config.HammerDatabase

    def dump_database(self) -> str:
        """Dump the current database JSON in a temporary file in the run_dir and return the path.
        """
        path = os.path.join(self.run_dir, "config_db_tmp.json")
        db_contents = self._database.get_database_json()
        with open(path, 'w') as f:
            f.write(db_contents)
        return path

    @property
    def config_dirs(self) -> List[str]:
        """
        List of folders where (default) configs can live.
        Defaults to self.tool_dir.

        :return: List of default config folders.
        """
        return [self.tool_dir]

    def get_config(self) -> List[dict]:
        """Get the config for this tool."""
        return reduce(add_lists, map(lambda path: hammer_config.load_config_from_defaults(path), self.config_dirs))

    def get_setting(self, key: str, nullvalue: Any = None) -> Any:
        """
        Get a particular setting from the database.

        :param key: Key of the setting to receive.
        :param nullvalue: Value to return in case of null (leave as None to use the default).
        """
        try:
            return self._database.get_setting(key, nullvalue)
        except AttributeError:
            raise ValueError("Internal error: no database set by hammer-vlsi")

    def set_setting(self, key: str, value: Any) -> None:
        """
        Set a runtime setting in the database.
        """
        self._database.set_setting(key, value)

    def create_enter_script(self, enter_script_location: str = "", raw: bool = False) -> None:
        """
        Create the enter script inside the rundir which can be used to
        create an interactive environment with all the same variables
        used to launch this tool.

        :param enter_script_location: Location to create the enter script. Defaults to self.run_dir + "/enter"
        :param raw: Emit the raw string without shell escaping (without quotes!!!)
        """
        def escape_value(val: str) -> str:
            if raw:
                return val
            else:
                if val == "":
                    return '""'
                quoted = shlex.quote(val) # type: str
                # For readability e.g. export X="9" vs export X=9
                if quoted == val:
                    return '"' + val + '"'
                else:
                    return quoted

        if enter_script_location == "":
            enter_script_location = os.path.join(self.run_dir, "enter")
        enter_script = "\n".join(map(lambda k_v: "export {0}={1}".format(k_v[0], escape_value(k_v[1])), sorted(self.env_vars.items())))
        with open(enter_script_location, "w") as f:
            f.write(enter_script)

    def check_input_files(self, extensions: List[str]) -> bool:
        """Verify that input files exist and have the specified extensions.

        :param extensions: List of extensions e.g. [".v", ".sv"]
        :return: True if all files exist and have the specified extensions.
        """
        verilog_args = self.input_files
        error = False
        for v in verilog_args:
            if not v.endswith(tuple(extensions)):
                self.logger.error("Input of unsupported type {0} detected!".format(v))
                error = True
            if not os.path.isfile(v):
                self.logger.error("Input file {0} does not exist!".format(v))
                error = True
        return not error

    def filter_for_mmmc(self, voltage: VoltageValue, temp: TemperatureValue) -> Callable[[hammer_tech.Library],bool]:
        """
        Selecting libraries that match given temp and voltage.
        """
        def extraction_func(lib: hammer_tech.Library) -> bool:
            if lib.corner is None or lib.corner.temperature is None:
                return False
            if lib.supplies is None or lib.supplies.VDD is None:
                return False
            lib_temperature = TemperatureValue(str(lib.corner.temperature))
            lib_VDD = VoltageValue(str(lib.supplies.VDD))
            if lib_temperature == temp:
                if lib_VDD == voltage:
                    return True
                else:
                    return False
            else:
                return False
        return extraction_func

    @staticmethod
    def replace_tcl_set(variable: str, value: str, tcl_path: str, quotes: bool = True) -> None:
        """
        Utility function to replaces a "set VARIABLE ..." line with set VARIABLE
        "value" in the given TCL script file.

        :param variable: Variable name to replace
        :param value: Value to replace it with (default quoted)
        :param tcl_path: Path to the TCL script.
        :param quotes: (optional) Set to False to disable quoting of the value.
        """
        with open(tcl_path, "r") as f:
            tcl_contents = f.read() # type: str

        value_string = value
        if quotes:
            value_string = '"' + value_string + '"'
        replacement_string = "set %s %s;" % (variable, value_string)

        regex = r'^set +%s.*' % (re.escape(variable))
        if re.search(regex, tcl_contents, flags=re.MULTILINE) is None:
            raise ValueError("set %s line not found in tcl file %s!" % (variable, tcl_path))

        new_tcl_contents = re.sub(regex, replacement_string, tcl_contents, flags=re.MULTILINE) # type: str

        with open(tcl_path, "w") as f:
            f.write(new_tcl_contents)

    # TODO(edwardw): consider pulling this out so that hammer_tech can also use this
    def run_executable(self, args: List[str], cwd: str = None) -> str:
        """
        Run an executable and log the command to the log while also capturing the output.

        :param args: Command-line to run; each item in the list is one token. The first token should be the command to run.
        :param cwd: Working directory (leave as None to use the current working directory).
        :return: Output from the command or an error message.
        """

        return self.submit_command.submit(args, self._subprocess_env, self.logger, cwd)

    # TODO: these helper functions might get a bit out of hand, put them somewhere more organized?
    def get_clock_ports(self) -> List[ClockPort]:
        """
        Get the clock ports of the top-level module, as specified in vlsi.inputs.clocks.
        """
        clocks = self.get_setting("vlsi.inputs.clocks")
        output = [] # type: List[ClockPort]
        for clock_port in clocks:
            clock = ClockPort(
                name=clock_port["name"], period=TimeValue(clock_port["period"]),
                uncertainty=None, path=None, generated=None, source_path=None, divisor=None
            )
            if "path" in clock_port:
                clock = clock._replace(path=clock_port["path"])
            if "uncertainty" in clock_port:
                clock = clock._replace(uncertainty=TimeValue(clock_port["uncertainty"]))
            generated = None  # type: Optional[bool]
            if "generated" in clock_port:
                generated = bool(clock_port["generated"])
                if generated:
                    clock = clock._replace(
                        source_path=clock_port["source_path"],
                        divisor=int(clock_port["divisor"])
                    )
            clock = clock._replace(generated=generated)
            output.append(clock)
        return output

    def get_all_supplies(self, key: str) -> List[Supply]:
        supplies = self.get_setting(key)
        output = []  # type: List[Supply]
        for raw_supply in supplies:
            supply = Supply(name=raw_supply['name'], pin=None, tie=None, weight=1)
            if 'pin' in raw_supply:
                supply = supply._replace(pin=raw_supply['pin'])
            if 'tie' in raw_supply:
                supply = supply._replace(tie=raw_supply['tie'])
            if 'weight' in raw_supply:
                supply = supply._replace(weight=raw_supply['weight'])
            output.append(supply)
        return output

    def get_all_power_nets(self) -> List[Supply]:
        return self.get_all_supplies("vlsi.inputs.supplies.power")

    def get_independent_power_nets(self) -> List[Supply]:
        return list(filter(lambda x: x.tie is None, self.get_all_power_nets()))

    def get_all_ground_nets(self) -> List[Supply]:
        return self.get_all_supplies("vlsi.inputs.supplies.ground")

    def get_independent_ground_nets(self) -> List[Supply]:
        return list(filter(lambda x: x.tie is None, self.get_all_ground_nets()))

    def get_bumps(self) -> Optional[BumpsDefinition]:
        bumps_mode = self.get_setting("vlsi.inputs.bumps_mode")
        if bumps_mode == "empty":
            return None
        elif bumps_mode != "manual":
            self.logger.error("Invalid bumps_mode:{m}, only empty or manual supported. Assuming empty.".format(
                m=bumps_mode))
            return None
        assignments = []  # type: List[BumpAssignment]
        for raw_assign in self.get_setting("vlsi.inputs.bumps.assignments"):
            name = None if not "name" in raw_assign else raw_assign["name"]
            no_con = False if not "no_connect" in raw_assign else raw_assign["no_connect"]
            x = raw_assign["x"]
            y = raw_assign["y"]
            cell = None if not "custom_cell" in raw_assign else raw_assign["custom_cell"]
            if name is None and not no_con:
                self.logger.warning("Invalid bump assignment, neither name nor no_connect specified for bump {x},{y}. Assuming it should be unassigned".format(
                    x=x, y=y))
            else:
                assignments.append(BumpAssignment(name=name, no_connect=no_con,
                    x=x, y=y, custom_cell=cell))
        return BumpsDefinition(x=self.get_setting("vlsi.inputs.bumps.x"),
            y=self.get_setting("vlsi.inputs.bumps.y"),
            pitch=self.get_setting("vlsi.inputs.bumps.pitch"),
            cell=self.get_setting("vlsi.inputs.bumps.cell"), assignments=assignments)

    def get_pin_assignments(self) -> List[PinAssignment]:
        """
        Get a list of pin assignments in accordance with settings in the Hammer IR.
        :return: A potentially empty list of PinAssigments.
        """
        pin_mode = str(self.get_setting("vlsi.inputs.pin_mode"))  # type: str
        if pin_mode == "none":
            return []
        elif pin_mode == "generated":
            pass
        else:
            self.logger.error(
                "Invalid pin_mode {mode}. Using none pin mode.".format(mode=pin_mode))
            return []

        # Generated pin mode needs to ingest the assignments
        assigns = []  # type: List[PinAssignment]
        for raw_assign in self.get_setting("vlsi.inputs.pin.assignments"):
            pins = str(raw_assign["pins"])  # type: str
            side = None if not "side" in raw_assign else raw_assign["side"]
            if not (side is None or side == "top" or side == "bottom" or side == "right" or side == "left") :
                self.logger.warning("Pins {p} have invalid side {s}. Assuming pins will be handled by CAD tool.".format(p=pins, s=side))
                continue
            preplaced = raw_assign.get("preplaced", False)
            layers = [] if not "layers" in raw_assign else raw_assign["layers"]
            if preplaced:
                if len(layers) != 0 or side is not None:
                    self.logger.warning("Pins {p} assigned as a preplaced pin with layers or side. Assuming pins are preplaced pins and ignoring layers and side.".format(p=pins))
                    assigns.append(PinAssignment(pins=pins, side=None, layers=[], preplaced=preplaced))
                    continue
            else:
                if len(layers) == 0 or side is None:
                    self.logger.warning("Pins {p} assigned without layers or side. Assuming pins will be handled by CAD tool.".format(p=pins))
                    # No pin appended
                    continue
            stackup = self.get_stackup()
            for layer in layers:
                direction = stackup.get_metal(layer).direction
                is_horizontal = direction == RoutingDirection.Horizontal and (side == "left" or side == "right")
                is_vertical = direction == RoutingDirection.Vertical and (side == "top" or side == "bottom")
                is_redis = direction == RoutingDirection.Redistribution
                if not (is_horizontal or is_vertical or is_redis):
                    self.logger.error("Pins {p} assigned layers {l} that do not match the direction of their side {s}. This is very likely to cause issues.".format(p=pins, l=layers, s=side))
            assigns.append(PinAssignment(pins=pins, side=side, layers=layers, preplaced=preplaced))
        return assigns

    def get_gds_map_file(self) -> Optional[str]:
        """
        Get a GDS map in accordance with settings in the Hammer IR.
        Return a fully-resolved (i.e. already prepended path) path to the GDS map or None if none was specified.
        :return: Fully-resolved path to GDS map file or None.
        """
        # Mode can be auto, empty, or manual
        gds_map_mode = str(self.get_setting("par.inputs.gds_map_mode"))  # type: str

        # gds_map_file will only be used in manual mode
        # Not including the map_file flag includes all layers but with no specific layer numbers
        manual_map_file = str(self.get_setting("par.inputs.gds_map_file")) if self.get_setting(
            "par.inputs.gds_map_file") is not None else None  # type: Optional[str]

        # tech_map_file will only be used in auto mode
        tech_map_file_raw = self.technology.config.gds_map_file  # type: ignore
        tech_map_file_optional = str(
            tech_map_file_raw) if tech_map_file_raw is not None else None  # type: Optional[str]
        tech_map_file = optional_map(tech_map_file_optional, lambda p: self.technology.prepend_dir_path(p))

        if gds_map_mode == "auto":
            map_file = tech_map_file
        elif gds_map_mode == "manual":
            map_file = manual_map_file
        elif gds_map_mode == "empty":
            map_file = None
        else:
            self.logger.error(
                "Invalid gds_map_mode {mode}. Using auto gds map.".format(mode=gds_map_mode))
            map_file = tech_map_file

        return map_file

    def get_dont_use_list(self) -> List[str]:
        """
        Get a "don't use" list in accordance with settings in the Hammer IR.
        Return a list of cells to mark as "don't use".
        :return: A list of cells to avoid using.
        """
        # Mode can be auto, manual, or append
        dont_use_mode = str(self.get_setting("vlsi.inputs.dont_use_mode"))  # type: str

        # dont_use_list will only be used in manual and append mode
        manual_dont_use_list = self.get_setting("vlsi.inputs.dont_use_list")  # type: List[str]
        assert isinstance(manual_dont_use_list, list), "vlsi.inputs.dont_use_list must be a list"

        # tech_dont_use_list will only be used in auto and append mode
        tech_dont_use_list = get_or_else(self.technology.dont_use_list, [])  # type: List[str]

        # Default to auto (use tech_dont_use_list).
        dont_use_list = tech_dont_use_list  # type: List[str]

        if dont_use_mode == "auto":
            pass
        elif dont_use_mode == "manual":
            dont_use_list = manual_dont_use_list
        elif dont_use_mode == "append":
            dont_use_list = tech_dont_use_list + manual_dont_use_list
        else:
            self.logger.error(
                "Invalid dont_use_mode {mode}. Using auto dont use list.".format(mode=dont_use_mode))

        return dont_use_list

    def get_placement_constraints(self) -> List[PlacementConstraint]:
        """
        Get a list of placement constraints as specified in the config.
        """
        constraints = self.get_setting("vlsi.inputs.placement_constraints")
        assert isinstance(constraints, list)
        return list(map(PlacementConstraint.from_dict, constraints))

    def get_mmmc_corners(self) -> List[MMMCCorner]:
        """
        Get a list of MMMC corners as specified in the config.
        """
        corners = self.get_setting("vlsi.inputs.mmmc_corners")
        output = []  # type: List[MMMCCorner]
        for corner in corners:
            corner_type = MMMCCornerType.from_string(str(corner["type"]))
            corn = MMMCCorner(
                name=str(corner["name"]),
                type=corner_type,
                voltage=VoltageValue(str(corner["voltage"])),
                temp=TemperatureValue(str(corner["temp"])),
            )
            output.append(corn)
        return output

    def get_stackup(self) -> Stackup:
        """
        Get the stackup provided by the technology key
        """
        # TODO how does python cache this? Do we need to avoid re-processing this every time?
        return self.technology.get_stackup_by_name(self.get_setting("technology.core.stackup"))

    def get_input_ilms(self) -> List[ILMStruct]:
        """
        Get a list of input ILM modules for hierarchical mode.
        """
        ilms = self.get_setting("vlsi.inputs.ilms")  # type: List[dict]
        assert isinstance(ilms, list)
        return list(map(ILMStruct.from_setting, ilms))

    def get_output_load_constraints(self) -> List[OutputLoadConstraint]:
        """
        Get a list of output load constraints as specified in the config.
        """
        output_loads = self.get_setting("vlsi.inputs.output_loads")
        output = []  # type: List[OutputLoadConstraint]
        for load_src in output_loads:
            load = OutputLoadConstraint(
                name=str(load_src["name"]),
                load=float(load_src["load"])
            )
            output.append(load)
        return output

    def get_delay_constraints(self) -> List[DelayConstraint]:
        """
        Get a list of input and output delay constraints as specified in
        the config.
        """
        delays = self.get_setting("vlsi.inputs.delays")  # type: List[dict]
        output = list(map(DelayConstraint.from_dict, delays))  # type: List[DelayConstraint]
        return output

    @staticmethod
    def append_contents_to_path(content_to_append: str, target_path: str) -> None:
        """
        Append the given contents to the file located at target_path, if target_path is not empty.

        :param content_to_append: Content to append.
        :param target_path: Where to append the content.
        """
        if content_to_append != "":
            content_lines = content_to_append.split("\n")  # type: List[str]

            # TODO(edwardw): come up with a more generic "source locator" for hammer
            header_text = "# The following snippet was added by HAMMER"
            content_lines.insert(0, header_text)

            with open(target_path, "a") as f:
                f.write("\n".join(content_lines))

    @staticmethod
    def tcl_append(cmd: str, output_buffer: List[str]) -> None:
        """
        Helper function to echo and run a command.

        :param cmd: TCL command to run
        :param output_buffer: Buffer in which to enqueue the resulting TCL lines.
        """
        output_buffer.append(cmd)

    @staticmethod
    def verbose_tcl_append(cmd: str, output_buffer: List[str]) -> None:
        """
        Helper function to echo and run a command.

        :param cmd: TCL command to run
        :param output_buffer: Buffer in which to enqueue the resulting TCL lines.
        """
        output_buffer.append("""puts "{0}" """.format(cmd.replace('"', '\"')))
        output_buffer.append(cmd)
