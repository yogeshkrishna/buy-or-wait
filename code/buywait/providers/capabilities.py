from buywait.contracts.models import ProviderCapability
from buywait.domain.enums import ProviderKind
from buywait.evidence.types import SCHEMA_VERSION, MODEL_SCHEMA_VERSION


def active_capabilities(ocr=None, model=None):
    result = [ProviderCapability("deterministic", ProviderKind.EVIDENCE, "2", True, True,
                                 ("message_to_facts",), (SCHEMA_VERSION,))]
    if ocr:
        result.append(ProviderCapability(ocr.provider_id, ProviderKind.OCR, ocr.version, True, True,
                                         ("image_to_text", "bounding_boxes"), (SCHEMA_VERSION,)))
    if model:
        result.append(ProviderCapability(model.provider_id, ProviderKind.MODEL, model.version, True, True,
                                         ("message_to_facts", "image_to_facts"), (MODEL_SCHEMA_VERSION,), requires_network=True,
                                         health_detail="Configured, not a live health check"))
    return tuple(result)
