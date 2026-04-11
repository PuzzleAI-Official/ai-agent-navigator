# ============================================================================
# Custom Exception Classes
# ============================================================================
# WHY CUSTOM EXCEPTIONS?
#   When something goes wrong, we want to know:
#     1. WHICH agent failed (agent_name)
#     2. WHICH user request it was processing (trace_id)
#     3. WHAT type of failure occurred (exception class)
#
#   Python's built-in exceptions (ValueError, RuntimeError) don't carry this
#   context. Custom exceptions let us attach agent_name and trace_id so the
#   logging system can include them automatically.
#
# HOW THEY'RE USED:
#   try:
#       result = run_user_understanding_agent(input_data)
#   except AgentRateLimitError:
#       # Wait and retry
#   except AgentAPIError:
#       # Log and alert
#   except AgentOutputError:
#       # Claude returned invalid structure — log for debugging
# ============================================================================


class AgentError(Exception):
    """
    Base exception for all PuzzleEval agent errors.

    Every agent-related error carries:
      - agent_name: which agent threw this (e.g., "user_understanding")
      - trace_id: the unique ID for this user request, used to correlate
                  logs across multiple agents in the pipeline
    """

    def __init__(self, message: str, agent_name: str = "", trace_id: str = ""):
        super().__init__(message)
        self.agent_name = agent_name
        self.trace_id = trace_id


class AgentRateLimitError(AgentError):
    """
    Raised when the Anthropic API returns a 429 (rate limit exceeded).

    This means we're sending too many requests too fast. The caller should
    wait and retry after the period indicated in the response headers.
    """
    pass


class AgentAPIError(AgentError):
    """
    Raised when the Anthropic API returns an unexpected error (5xx, network
    failure, timeout, etc.).

    This is NOT a problem with our code — it's an issue with the API itself.
    The caller should log it and potentially retry.
    """
    pass


class AgentOutputError(AgentError):
    """
    Raised when Claude returns a response that doesn't match our expected
    Pydantic schema, even though we used structured outputs.

    This should be very rare with structured outputs (the API guarantees
    schema compliance), but we handle it defensively. If this fires, it
    likely means our schema definition has a bug.
    """
    pass


class AgentFileParseError(AgentError):
    """
    Raised when we fail to read or parse an uploaded workflow file.

    Common causes:
      - File doesn't exist at the given path
      - File is corrupted or password-protected
      - Unsupported file format
    """
    pass
