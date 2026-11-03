"""Bounded, explicitly invoked OpenAI image generation for fictional tests."""
import base64
import binascii
import json
import re
from urllib import error, request

from django.core.files.uploadedfile import SimpleUploadedFile
from .ai_provider import ProviderError, _NoRedirect
from .site_images import MAX_UPLOAD_BYTES, InvalidImage, normalise_image

IMAGE_URL = 'https://api.openai.com/v1/images/generations'
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
SIZES = ('1536x1024', '1024x1024', '1024x1536')
MAX_BRIEF = 600


def _send_request(req):
    try:
        with request.build_opener(_NoRedirect()).open(req, timeout=150) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProviderError('response_too_large')
        return json.loads(raw.decode('utf-8'))
    except (error.URLError, TimeoutError, UnicodeError, ValueError, OSError) as exc:
        raise ProviderError('provider_unavailable') from exc


def generate_image(*, api_key, model, prompt, size, safety_identifier):
    if (not isinstance(api_key, str) or not api_key or '\r' in api_key or '\n' in api_key or
            not re.fullmatch(r'[A-Za-z0-9_.:-]{1,100}', model or '')):
        raise ProviderError('not_configured')
    if (not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_BRIEF or
            size not in SIZES or not re.fullmatch(r'[0-9a-f]{64}', safety_identifier or '')):
        raise ProviderError('invalid_request')
    payload = {'model':model, 'prompt':prompt, 'size':size, 'n':1, 'quality':'low',
               'output_format':'png', 'safety_identifier':safety_identifier}
    req = request.Request(IMAGE_URL, data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                          headers={'Authorization':'Bearer '+api_key, 'Content-Type':'application/json'}, method='POST')
    result = _send_request(req)
    try:
        images = result['data']
        if not isinstance(images, list) or len(images) != 1 or not isinstance(images[0], dict):
            raise ValueError('invalid response')
        encoded = images[0]['b64_json']
        if not isinstance(encoded, str) or len(encoded) > ((MAX_UPLOAD_BYTES + 2) // 3) * 4:
            raise ValueError('invalid image length')
        raw = base64.b64decode(encoded, validate=True)
        return normalise_image(SimpleUploadedFile('candidate.png', raw, content_type='image/png'))
    except (KeyError, TypeError, ValueError, binascii.Error, InvalidImage) as exc:
        raise ProviderError('invalid_response') from exc
