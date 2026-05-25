"""
Shared exception types for the Audiobook Librarian.
"""


class TokenLimitError(Exception):
    """
    Raised by an LLM validator when the model's response was cut off because
    the output hit the max_tokens ceiling.

    The token limit being reached almost always means the model returned a
    truncated (unparseable) JSON fragment.  The calling sync loop should stop
    immediately and surface a clear message to the user rather than silently
    skipping the book.
    """
    pass


class RateLimitError(Exception):
    """
    Raised by an LLM validator when the API returns an HTTP 429 or equivalent
    quota-exhausted response (e.g. Groq TPD limit, OpenAI RPM, Anthropic RPM).

    Unlike TokenLimitError (a single truncated response), a rate limit means
    *every* subsequent request will also fail.  The calling sync loop must
    stop immediately so the user can see the error rather than silently
    skipping every remaining book.
    """
    pass
