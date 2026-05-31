export type DocsNavItem = {
  id: string;
  label: string;
};

export type DocsCodeExample = {
  title: string;
  language: string;
  code: string;
};

export type DocsParamRow = {
  name: string;
  type: string;
  required: string;
  description: string;
};

export type DocsEndpoint = {
  method: "GET" | "POST";
  path: string;
  title: string;
  description: string;
  notes: string[];
};

export type DocsErrorCode = {
  code: string;
  meaning: string;
  action: string;
};

export const alphaDocsNav: DocsNavItem[] = [
  { id: "quickstart", label: "Quickstart" },
  { id: "python-sdk", label: "Python SDK" },
  { id: "authentication", label: "Authentication" },
  { id: "idempotency", label: "Idempotency" },
  { id: "endpoints", label: "Endpoints" },
  { id: "routing-controls", label: "Routing controls" },
  { id: "invoice-metadata", label: "Invoice metadata" },
  { id: "responses", label: "Responses" },
  { id: "errors", label: "Errors" },
  { id: "support", label: "Support" },
];

export const quickstartSteps = [
  "Get an alpha API key from Puzzle.",
  "Set your API key and alpha base URL in your environment.",
  "Send a sync invoice extraction request with an idempotency key.",
  "Store the returned request_id for debugging and support.",
];

export const codeExamples: Record<string, DocsCodeExample> = {
  installSdk: {
    title: "Install Python SDK",
    language: "bash",
    code: `pip install puzzleai`,
  },
  syncInvoice: {
    title: "Sync invoice extraction",
    language: "curl",
    code: `export PUZZLE_API_KEY="YOUR_API_KEY"
export PUZZLE_BASE_URL="YOUR_ALPHA_BASE_URL"

curl -X POST "$PUZZLE_BASE_URL/v1/documents/invoices:extract" \\
  -H "Authorization: Bearer $PUZZLE_API_KEY" \\
  -H "Idempotency-Key: invoice-demo-001" \\
  -F "file=@invoice.pdf;type=application/pdf" \\
  -F 'metadata={"line_items_mode":"preferred","required_fields":["total"]}'`,
  },
  asyncInvoice: {
    title: "Async invoice extraction",
    language: "curl",
    code: `curl -X POST "$PUZZLE_BASE_URL/v1/documents/invoices:submit" \\
  -H "Authorization: Bearer $PUZZLE_API_KEY" \\
  -H "Idempotency-Key: invoice-demo-async-001" \\
  -F "file=@invoice.pdf;type=application/pdf" \\
  -F 'metadata={"line_items_mode":"preferred"}'

curl -X GET "$PUZZLE_BASE_URL/v1/jobs/YOUR_JOB_ID" \\
  -H "Authorization: Bearer $PUZZLE_API_KEY"`,
  },
  routingControls: {
    title: "Route through an approved provider set",
    language: "curl",
    code: `curl -X POST "$PUZZLE_BASE_URL/v1/documents/invoices:extract" \\
  -H "Authorization: Bearer $PUZZLE_API_KEY" \\
  -H "Idempotency-Key: invoice-routing-demo-001" \\
  -F "file=@invoice.pdf;type=application/pdf" \\
  -F 'metadata={
    "provider_set": "YOUR_PROVIDER_SET",
    "strategy": "highest_quality",
    "line_items_mode": "preferred"
  }'`,
  },
  explicitProvider: {
    title: "Explicit provider route",
    language: "curl",
    code: `curl -X POST "$PUZZLE_BASE_URL/v1/documents/invoices:extract" \\
  -H "Authorization: Bearer $PUZZLE_API_KEY" \\
  -H "Idempotency-Key: invoice-provider-demo-001" \\
  -F "file=@invoice.pdf;type=application/pdf" \\
  -F 'metadata={
    "provider_set": "YOUR_PROVIDER_SET",
    "provider": "PROVIDER_ID",
    "service_id": "SERVICE_ID",
    "line_items_mode": "preferred"
  }'`,
  },
  pythonSdk: {
    title: "Sync invoice extraction",
    language: "python",
    code: `from puzzleai import Client

client = Client(
    api_key="YOUR_API_KEY",
    base_url="YOUR_ALPHA_BASE_URL",
)

with open("invoice.pdf", "rb") as file:
    result = client.documents.invoices.extract(
        file=file,
        line_items_mode="preferred",
        required_fields=["total"],
        idempotency_key="invoice-demo-001",
    )

print(result["request_id"])
print(result["invoice"]["total"]["value"])`,
  },
  pythonSdkInvoiceOptions: {
    title: "Invoice extraction with routing options",
    language: "python",
    code: `from puzzleai import Client

client = Client(api_key="YOUR_API_KEY", base_url="YOUR_ALPHA_BASE_URL")

with open("invoice.pdf", "rb") as file:
    result = client.documents.invoices.extract(
        file=file,
        filename="invoice.pdf",
        content_type="application/pdf",
        provider_set="YOUR_PROVIDER_SET",
        strategy="highest_quality",
        provider="PROVIDER_ID",
        service_id="SERVICE_ID",
        line_items_mode="preferred",
        required_fields=["total"],
        provider_options={},
        features={},
        idempotency_key="invoice-route-demo-001",
    )

print(result["provider_id"])
print(result["quality"])`,
  },
  pythonSdkAsync: {
    title: "Async submit and job polling",
    language: "python",
    code: `from puzzleai import Client

client = Client(
    api_key="YOUR_API_KEY",
    base_url="YOUR_ALPHA_BASE_URL",
)

with open("invoice.pdf", "rb") as file:
    job = client.documents.invoices.submit(
        file=file,
        line_items_mode="preferred",
        idempotency_key="invoice-demo-async-001",
    )

result = client.jobs.get(job["job_id"])
print(result["status"])`,
  },
  pythonSdkDocumentProcess: {
    title: "General document processing",
    language: "python",
    code: `from puzzleai import Client

client = Client(api_key="YOUR_API_KEY", base_url="YOUR_ALPHA_BASE_URL")

with open("document.pdf", "rb") as file:
    result = client.documents.process(
        file=file,
        task="parse",
        required_capabilities=["documents.text"],
        optional_capabilities=["documents.layout", "documents.tables"],
        strategy="balanced",
        idempotency_key="document-parse-demo-001",
    )

print(result["request_id"])
print(result.get("full_text"))`,
  },
  pythonSdkAsyncClient: {
    title: "Async Python client",
    language: "python",
    code: `from puzzleai import AsyncClient

async with AsyncClient(
    api_key="YOUR_API_KEY",
    base_url="YOUR_ALPHA_BASE_URL",
) as client:
    with open("invoice.pdf", "rb") as file:
        result = await client.documents.invoices.extract(
            file=file,
            line_items_mode="preferred",
            idempotency_key="async-invoice-demo-001",
        )

    print(result["request_id"])`,
  },
  pythonSdkErrors: {
    title: "Handle SDK errors",
    language: "python",
    code: `from puzzleai import (
    Client,
    PuzzleError,
    PuzzleIdempotencyConflictError,
    PuzzleProviderUnavailableError,
    PuzzleRateLimitError,
    PuzzleValidationError,
)

client = Client(api_key="YOUR_API_KEY", base_url="YOUR_ALPHA_BASE_URL")

try:
    with open("invoice.pdf", "rb") as file:
        result = client.documents.invoices.extract(
            file=file,
            idempotency_key="invoice-demo-001",
        )
except PuzzleRateLimitError as error:
    print("Retry later:", error.request_id)
except PuzzleIdempotencyConflictError:
    print("Use a new idempotency key or replay the exact same request.")
except PuzzleProviderUnavailableError:
    print("No eligible provider was available.")
except PuzzleValidationError as error:
    print("Request validation failed:", error.details)
except PuzzleError as error:
    print(error.code)
    print(error.request_id)`,
  },
  response: {
    title: "Normalized invoice response",
    language: "json",
    code: `{
  "request_id": "req_alpha_123",
  "document_id": "doc_alpha_123",
  "workflow": "invoice.extract",
  "workflow_version": "invoice.extract.v1",
  "provider_id": "selected_provider",
  "service_id": "selected_service",
  "invoice": {
    "vendor_name": {
      "value": "Acme Supplies",
      "raw_value": "Acme Supplies",
      "confidence": 0.98
    },
    "invoice_number": {
      "value": "INV-1001",
      "raw_value": "INV-1001",
      "confidence": 0.97
    },
    "total": {
      "value": "108.25",
      "raw_value": "USD 108.25",
      "confidence": 0.99
    },
    "line_items": [
      {
        "description": {
          "value": "Widget",
          "confidence": 0.94
        },
        "quantity": {
          "value": "2",
          "confidence": 0.92
        },
        "amount": {
          "value": "100.00",
          "confidence": 0.93
        }
      }
    ]
  },
  "quality": {
    "accepted": true,
    "completeness_score": 0.86,
    "missing_required_fields": []
  },
  "warnings": [],
  "usage": {
    "cost_units": 6,
    "charge_units": 6,
    "provider": "selected_provider",
    "service_id": "selected_service"
  },
  "replayed": false
}`,
  },
  error: {
    title: "Error response",
    language: "json",
    code: `{
  "error": {
    "code": "validation_failed",
    "message": "Idempotency key is required",
    "request_id": "req_alpha_123",
    "details": {}
  }
}`,
  },
};

export const endpoints: DocsEndpoint[] = [
  {
    method: "POST",
    path: "/v1/documents/invoices:extract",
    title: "Extract an invoice synchronously",
    description:
      "Upload one document and receive a normalized invoice response in the same request.",
    notes: [
      "Requires an API key.",
      "Requires an Idempotency-Key header.",
      "Best for smaller files and interactive workflows.",
    ],
  },
  {
    method: "POST",
    path: "/v1/documents/invoices:submit",
    title: "Submit an invoice extraction job",
    description:
      "Upload one document and receive a job_id that can be polled until completion.",
    notes: [
      "Requires an API key.",
      "Requires an Idempotency-Key header.",
      "Use this for larger files or workflows that should not block on provider latency.",
    ],
  },
  {
    method: "GET",
    path: "/v1/jobs/{job_id}",
    title: "Get job status",
    description:
      "Poll an async job until it reaches succeeded, failed, or cancelled.",
    notes: [
      "Requires the same tenant API key that created the job.",
      "Succeeded jobs include the normalized result.",
    ],
  },
  {
    method: "POST",
    path: "/v1/documents:process",
    title: "Process a document",
    description:
      "Run the general document processing workflow for normalized text, fields, and tables where available.",
    notes: [
      "Requires an API key.",
      "Requires an Idempotency-Key header.",
      "Invoice-specific users should prefer the invoice endpoints.",
    ],
  },
  {
    method: "POST",
    path: "/v1/documents:submit",
    title: "Submit a document processing job",
    description:
      "Run general document processing asynchronously and poll the job endpoint for results.",
    notes: [
      "Requires an API key.",
      "Requires an Idempotency-Key header.",
    ],
  },
];

export const headerParams: DocsParamRow[] = [
  {
    name: "Authorization",
    type: "string",
    required: "required",
    description: "Use Bearer authentication: Authorization: Bearer YOUR_API_KEY.",
  },
  {
    name: "X-API-Key",
    type: "string",
    required: "optional",
    description: "Alternative API key header for environments that cannot set Authorization.",
  },
  {
    name: "Idempotency-Key",
    type: "string",
    required: "required",
    description: "Unique key per billable operation. Reuse the same key to safely replay.",
  },
];

export const multipartParams: DocsParamRow[] = [
  {
    name: "file",
    type: "file",
    required: "required",
    description: "PDF, PNG, JPEG, or TIFF document to process.",
  },
  {
    name: "metadata",
    type: "JSON string",
    required: "optional",
    description: "Workflow options such as line_items_mode, required_fields, strategy, provider, or service_id.",
  },
];

export const invoiceMetadataParams: DocsParamRow[] = [
  {
    name: "line_items_mode",
    type: "preferred | required | disabled",
    required: "optional",
    description: "Controls whether line items are a preference, hard requirement, or ignored. Default: preferred.",
  },
  {
    name: "required_fields",
    type: "string[]",
    required: "optional",
    description: "Fields that must be present for the workflow to accept the result. Default: [\"total\"].",
  },
];

export const sdkInvoiceOptionParams: DocsParamRow[] = [
  {
    name: "file",
    type: "path | bytes | file object",
    required: "required",
    description: "Document bytes to upload. Most users pass an open binary file handle.",
  },
  {
    name: "filename",
    type: "string",
    required: "optional",
    description: "Overrides the upload filename when passing bytes or an unnamed file object.",
  },
  {
    name: "content_type",
    type: "string",
    required: "optional",
    description: "MIME type for the upload. Default: application/pdf.",
  },
  {
    name: "line_items_mode",
    type: "preferred | required | disabled",
    required: "optional",
    description: "Controls whether invoice line items are preferred, required, or ignored.",
  },
  {
    name: "required_fields",
    type: "string[]",
    required: "optional",
    description: "Invoice fields that must be present for an accepted workflow result.",
  },
  {
    name: "strategy",
    type: "balanced | cheapest | highest_quality",
    required: "optional",
    description: "Routing preference when multiple verified services are eligible.",
  },
  {
    name: "provider",
    type: "string",
    required: "optional",
    description: "Restrict the request to one provider in the selected provider set.",
  },
  {
    name: "service_id",
    type: "string",
    required: "optional",
    description: "Restrict the request to one service for the selected provider.",
  },
  {
    name: "provider_set",
    type: "string",
    required: "optional",
    description: "Named account-approved provider allowlist. If omitted, Puzzle uses your account default.",
  },
  {
    name: "provider_options",
    type: "object",
    required: "optional",
    description: "Provider-specific options approved for your alpha account.",
  },
  {
    name: "features",
    type: "object",
    required: "optional",
    description: "Optional routing hints approved for your account. Most requests can omit this.",
  },
  {
    name: "idempotency_key",
    type: "string",
    required: "required",
    description: "Stable key for safe retries and replay-safe billing.",
  },
];

export const sdkDocumentOptionParams: DocsParamRow[] = [
  {
    name: "task",
    type: "string",
    required: "optional",
    description: "Document task to run. Default: parse.",
  },
  {
    name: "required_capabilities",
    type: "string[]",
    required: "optional",
    description: "Capabilities the selected service must satisfy, such as documents.text.",
  },
  {
    name: "optional_capabilities",
    type: "string[]",
    required: "optional",
    description: "Capabilities that improve scoring but are not hard requirements.",
  },
  {
    name: "provider / service_id",
    type: "string",
    required: "optional",
    description: "Optional explicit provider or provider service route.",
  },
  {
    name: "strategy",
    type: "balanced | cheapest | highest_quality",
    required: "optional",
    description: "Routing strategy for eligible provider services.",
  },
  {
    name: "idempotency_key",
    type: "string",
    required: "required",
    description: "Stable key for safe retries and replay-safe billing.",
  },
];

export const routingControlParams: DocsParamRow[] = [
  {
    name: "provider_set",
    type: "string",
    required: "optional",
    description: "Named account-approved provider allowlist. If omitted, Puzzle uses your account default.",
  },
  {
    name: "strategy",
    type: "balanced | cheapest | highest_quality",
    required: "optional",
    description: "Routing preference used when multiple verified services are eligible. Default: balanced.",
  },
  {
    name: "provider",
    type: "string",
    required: "optional",
    description: "Restrict the request to one approved provider inside the selected provider set.",
  },
  {
    name: "service_id",
    type: "string",
    required: "optional",
    description: "Restrict the request to one service for the selected provider.",
  },
  {
    name: "provider_options",
    type: "object",
    required: "optional",
    description: "Provider-specific options approved for your alpha account.",
  },
];

export const responseFields: DocsParamRow[] = [
  {
    name: "request_id",
    type: "string",
    required: "always",
    description: "Stable identifier to include in support requests.",
  },
  {
    name: "document_id",
    type: "string",
    required: "always",
    description: "Identifier for the uploaded document inside Puzzle.",
  },
  {
    name: "provider_id / service_id",
    type: "string",
    required: "always",
    description: "The selected provider service that produced the accepted result.",
  },
  {
    name: "invoice",
    type: "object",
    required: "invoice endpoint",
    description: "Canonical invoice fields such as vendor, dates, total, tax, and line items.",
  },
  {
    name: "quality",
    type: "object",
    required: "invoice endpoint",
    description: "Acceptance status, completeness score, and missing required fields.",
  },
  {
    name: "warnings",
    type: "string[]",
    required: "always",
    description: "Non-fatal parsing, normalization, or quality notes.",
  },
  {
    name: "usage",
    type: "object",
    required: "always",
    description: "Normalized usage units for the accepted result.",
  },
  {
    name: "replayed",
    type: "boolean",
    required: "always",
    description: "True when Puzzle returned a stored idempotency result.",
  },
];

export const errorCodes: DocsErrorCode[] = [
  {
    code: "authentication_failed",
    meaning: "The API key is missing or invalid.",
    action: "Check the key and header format.",
  },
  {
    code: "authorization_failed",
    meaning: "The key is valid but cannot access this tenant resource.",
    action: "Use the key assigned to the correct alpha tenant.",
  },
  {
    code: "validation_failed",
    meaning: "The request body, file, metadata, or headers are invalid.",
    action: "Check required headers, metadata JSON, and file type.",
  },
  {
    code: "rate_limited",
    meaning: "The alpha tenant exceeded its request rate.",
    action: "Back off and retry later.",
  },
  {
    code: "quota_exceeded",
    meaning: "The alpha tenant exceeded its configured usage quota.",
    action: "Contact Puzzle to adjust the alpha quota.",
  },
  {
    code: "idempotency_conflict",
    meaning: "The same idempotency key was reused with different request content.",
    action: "Use a new key for a different operation.",
  },
  {
    code: "provider_unavailable",
    meaning: "No eligible provider service produced an acceptable result.",
    action: "Retry later or contact Puzzle with the request_id.",
  },
  {
    code: "timeout",
    meaning: "A provider or workflow exceeded its timeout window.",
    action: "Use async submit for larger files or retry later.",
  },
  {
    code: "dependency_unavailable",
    meaning: "A required Puzzle dependency was unavailable.",
    action: "Retry later. Puzzle fails closed for new billable work.",
  },
  {
    code: "internal_error",
    meaning: "Unexpected server error.",
    action: "Contact Puzzle with the request_id and timestamp.",
  },
];

export const supportChecklist = [
  "request_id",
  "job_id, if the request was async",
  "timestamp and timezone",
  "endpoint used",
  "file type and approximate size",
  "which expected field looked wrong or missing",
];
