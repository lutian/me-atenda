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


def public_url(base, path, query=b''):
    # Never trust Host or X-Forwarded-* headers; preserve original query bytes/order.
    return base + path + ('?' + query.decode('ascii') if query else '')
