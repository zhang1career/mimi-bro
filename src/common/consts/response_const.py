"""
Error code definitions
"""

# =========================
# Base
# =========================

RET_OK = 0                  # success
RET_ERR = 1                 # generic error
RET_UNKNOWN = 2             # unknown error
RET_NOT_IMPLEMENTED = 3     # feature not implemented


# =========================
# Request & Parameters (100–199)
# =========================

RET_INVALID_PARAM = 100         # invalid parameter
RET_MISSING_PARAM = 101         # missing required parameter
RET_PARAM_FORMAT_ERROR = 102    # parameter format error
RET_PARAM_OUT_OF_RANGE = 103    # parameter out of range
RET_JSON_PARSE_ERROR = 104      # json parse error


# =========================
# Auth & Permission (200–299)
# =========================

RET_UNAUTHORIZED = 200          # unauthorized
RET_FORBIDDEN = 201             # forbidden
RET_LOGIN_REQUIRED = 202        # login required

RET_TOKEN_MISSING = 210         # token missing
RET_TOKEN_INVALID = 211         # token invalid
RET_TOKEN_EXPIRED = 212         # token expired
RET_TOKEN_REVOKED = 213         # token revoked


# =========================
# Business Logic (300–399)
# =========================

RET_BUSINESS_ERROR = 300         # generic business error
RET_RESOURCE_NOT_FOUND = 301     # resource not found
RET_RESOURCE_EXISTS = 302        # resource already exists
RET_INVALID_STATE = 303          # invalid state
RET_DUPLICATE_REQUEST = 304      # duplicate request
RET_OPERATION_NOT_ALLOWED = 305  # operation not allowed


# =========================
# External Dependency (400–499)
# =========================

RET_DEPENDENCY_ERROR = 400         # dependency error

RET_HTTP_TIMEOUT = 401             # http request timeout
RET_HTTP_5XX = 402                 # http 5xx error
RET_HTTP_RESPONSE_INVALID = 403    # invalid http response

RET_MQ_ERROR = 410                 # message queue error
RET_REDIS_ERROR = 420              # redis error
RET_THIRD_PARTY_ERROR = 430        # third-party service error


# =========================
# Data & Storage (500–599)
# =========================

RET_DB_ERROR = 500               # database error
RET_DB_CONNECTION_FAILED = 501   # database connection failed
RET_DB_QUERY_FAILED = 502        # database query failed
RET_DB_DUPLICATE_KEY = 503       # duplicate key
RET_DB_TRANSACTION_FAILED = 504  # transaction failed

RET_FILE_IO_ERROR = 520          # file io error
RET_OBJECT_STORAGE_ERROR = 530   # object storage error


# =========================
# Concurrency & Protection (600–699)
# =========================

RET_CONCURRENCY_ERROR = 600      # concurrency error
RET_LOCK_FAILED = 601            # lock acquire failed
RET_IDEMPOTENT_CONFLICT = 602    # idempotent conflict

RET_RATE_LIMITED = 610           # rate limited
RET_CIRCUIT_OPEN = 611           # circuit breaker open

RET_THREAD_ERROR = 660           # thread timeout
RET_THREAD_TIMEOUT = 661         # thread timeout


# =========================
# AI / Agent (700–799)
# =========================

RET_AI_ERROR = 700                # generic ai error
RET_PROMPT_PARSE_FAILED = 701     # prompt parse failed
RET_PLAN_GENERATION_FAILED = 702  # execution plan generation failed
RET_TOOL_NOT_FOUND = 703          # tool not found
RET_TOOL_EXECUTION_FAILED = 704   # tool execution failed
RET_MODEL_RESPONSE_INVALID = 705  # invalid model response
RET_AI_POLICY_VIOLATION = 706     # ai policy violation


# =========================
# Ops & Environment (800–899)
# =========================

RET_CONFIG_ERROR = 800            # configuration error
RET_ENV_NOT_READY = 801           # environment not ready
RET_SERVICE_UNAVAILABLE = 802     # service unavailable
RET_DEPLOYMENT_ERROR = 803        # deployment error
RET_MAINTENANCE_MODE = 804        # maintenance mode active

# =========================
