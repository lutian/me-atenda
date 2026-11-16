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
    """Configured scheme only; Twilio documents a trailing-slash compatibility retry.

    This is not evidence of the signing scheme used by a real handshake. Only
    the final path slash varies; origin, configured prefix and raw query do not.
    HTTP callbacks deliberately continue using signature_valid without retries.
    """
    if signature_valid(token, public_url(base, path, query), {}, signature): return True
    alternate = path[:-1] if path.endswith('/') else path + '/'
    return signature_valid(token, public_url(base, alternate, query), {}, signature)


def public_url(base, path, query=b''):
    # Never trust Host or X-Forwarded-* headers; preserve original query bytes/order.
    return base + path + ('?' + query.decode('ascii') if query else '')
