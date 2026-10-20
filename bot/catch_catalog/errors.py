class ResearchError(Exception):
    """An operator-readable collection refusal, with no sensitive payload."""


class Busy(ResearchError):
    pass


class PolicyError(ResearchError):
    pass


class IntegrityError(ResearchError):
    pass


class DiskPressure(ResearchError):
    pass


class TransferError(ResearchError):
    pass
