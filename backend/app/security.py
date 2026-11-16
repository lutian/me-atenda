"""Twilio's form-signature algorithm with the configured exact public URL."""
import base64
import hashlib
import hmac


def signature_valid(token, url, form, signature):
    if not token or not signature: return False
    value = url
    for key in sorted(set(form)):
        values = form.getlist(key) if hasattr(form, 'getlist') else [form[key]]
        for item in sorted(set(values)):
            value += key + str(item)
    expected = base64.b64encode(hmac.new(token.encode(), value.encode(), hashlib.sha1).digest()).decode()
    return hmac.compare_digest(expected.encode(), signature.encode())


def websocket_signature_valid(token, base, path, query, signature):
    """Authenticate the configured WSS endpoint, with same-endpoint HTTPS fallback.

    TwiML emits WSS from the configured HTTPS base. Only that scheme mapping and
    the final path slash may vary; origin, port, prefix and raw query stay fixed.
    HTTP callbacks continue using signature_valid without any compatibility retry.
    """
    if not base.startswith('https://'): return False
    alternate = path[:-1] if path.endswith('/') else path + '/'
    for candidate_base in ('wss://' + base[len('https://'):], base):
        for candidate_path in (path, alternate):
            if signature_valid(token, public_url(candidate_base, candidate_path, query), {}, signature):
                return True
    return False


def public_url(base, path, query=b''):
    # Never trust Host or X-Forwarded-* headers; preserve original query bytes/order.
    return base + path + ('?' + query.decode('ascii') if query else '')
