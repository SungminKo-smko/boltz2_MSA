from enum import StrEnum


class AssetKind(StrEnum):
    structure = "structure"
    msa = "msa"
    yaml = "yaml"
    template_cif = "template_cif"


class SpecSourceType(StrEnum):
    template = "template"
    raw_yaml = "raw_yaml"


class ValidationStatus(StrEnum):
    pending = "pending"
    valid = "valid"
    invalid = "invalid"
    error = "error"


class PredictionType(StrEnum):
    structure = "structure"
    affinity = "affinity"
    structure_affinity = "structure+affinity"
    virtual_screening = "virtual_screening"


class JobStatus(StrEnum):
    queued = "queued"
    running = "running"
    uploading = "uploading"
    succeeded = "succeeded"
    failed = "failed"
    canceled = "canceled"
