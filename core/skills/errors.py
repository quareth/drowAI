"""Typed failures for built-in skill discovery, loading, and resolution."""


class SkillError(Exception):
    """Base class for skill lifecycle failures."""


class SkillLoadError(SkillError):
    """Raised when a skill package cannot be read safely."""


class SkillParseError(SkillError):
    """Raised when a skill entrypoint cannot be parsed."""


class SkillValidationError(SkillError):
    """Raised when parsed skill content violates the package contract."""


class SkillRegistryError(SkillError):
    """Raised when loaded skill identities cannot form one registry."""


class SkillResolutionError(SkillError):
    """Raised when required skill content cannot fit the runtime policy."""


__all__ = [
    "SkillError",
    "SkillLoadError",
    "SkillParseError",
    "SkillRegistryError",
    "SkillResolutionError",
    "SkillValidationError",
]
