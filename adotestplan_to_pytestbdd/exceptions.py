from gherkin.errors import ParserError


class NoTestSuiteError(ValueError):
    pass


class OrderOfOperationsError(RuntimeError):
    pass


class MissingFixturesError(RuntimeError):
    pass


class InvalidStepError(RuntimeError):
    pass


class InvalidParameterError(RuntimeError):
    pass


class InvalidGherkinError(ParserError):
    pass
