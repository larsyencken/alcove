import subprocess


class ValidationError(Exception):
    "When a shelf config or step definition is invalid."

    pass


class StepDefinitionError(Exception):
    "When a step definition is invalid."

    pass


class StepScriptError(subprocess.CalledProcessError):
    "When a step's script exits non-zero or is killed by a signal."

    pass
