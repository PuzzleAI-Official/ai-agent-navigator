import type { DocsCodeExample, DocsNavItem, DocsParamRow } from "@/data/alphaApiDocs";

export const alphaSdkDocsNav: DocsNavItem[] = [
  { id: "quickstart", label: "Quickstart" },
  { id: "client", label: "Client setup" },
  { id: "invoices", label: "Invoices" },
  { id: "documents", label: "Documents" },
  { id: "async", label: "Async jobs" },
  { id: "routing", label: "Routing options" },
  { id: "errors", label: "Errors" },
  { id: "support", label: "Support" },
];

export const sdkQuickstartSteps = [
  "Install the package from PyPI.",
  "Create a Client with your API key and assigned alpha base URL.",
  "Pass an idempotency key for each billable operation.",
  "Store request_id or job_id for debugging and support.",
];

export const sdkCodeExamples: Record<string, DocsCodeExample> = {
  install: {
    title: "Install",
    language: "bash",
    code: `pip install puzzleai`,
  },
  syncInvoice: {
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
  envClient: {
    title: "Use environment variables",
    language: "python",
    code: `import os
from puzzleai import Client

os.environ["PUZZLE_BASE_URL"] = "YOUR_ALPHA_BASE_URL"

client = Client(api_key="YOUR_API_KEY")`,
  },
  contextManager: {
    title: "Close connections cleanly",
    language: "python",
    code: `from puzzleai import Client

with Client(api_key="YOUR_API_KEY", base_url="YOUR_ALPHA_BASE_URL") as client:
    status = client.jobs.get("YOUR_JOB_ID")
    print(status["status"])`,
  },
  invoiceOptions: {
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
  documentProcess: {
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
  asyncSubmit: {
    title: "Submit and poll an invoice job",
    language: "python",
    code: `from puzzleai import Client

client = Client(api_key="YOUR_API_KEY", base_url="YOUR_ALPHA_BASE_URL")

with open("invoice.pdf", "rb") as file:
    job = client.documents.invoices.submit(
        file=file,
        line_items_mode="preferred",
        idempotency_key="invoice-demo-async-001",
    )

result = client.jobs.get(job["job_id"])
print(result["status"])`,
  },
  asyncClient: {
    title: "Async client",
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
  errors: {
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
};

export const sdkClientParams: DocsParamRow[] = [
  {
    name: "api_key",
    type: "string",
    required: "required",
    description: "Puzzle API key assigned to your alpha account.",
  },
  {
    name: "base_url",
    type: "string",
    required: "recommended",
    description: "Alpha API base URL assigned to your account. You can also set PUZZLE_BASE_URL.",
  },
  {
    name: "timeout",
    type: "float | httpx.Timeout",
    required: "optional",
    description: "Request timeout. Default is suitable for normal alpha workflows.",
  },
  {
    name: "max_retries",
    type: "int",
    required: "optional",
    description: "Retries for safe transient failures. Requests without idempotency keys are not retried unless they are GET requests.",
  },
  {
    name: "retry_backoff",
    type: "float",
    required: "optional",
    description: "Initial exponential backoff delay in seconds.",
  },
];

export const sdkInvoiceParams: DocsParamRow[] = [
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
    description: "Invoice fields that must be present for an accepted result.",
  },
  {
    name: "idempotency_key",
    type: "string",
    required: "required",
    description: "Stable key for safe retries and replay-safe billing.",
  },
];

export const sdkDocumentParams: DocsParamRow[] = [
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
    description: "Capabilities that improve selection but are not hard requirements.",
  },
  {
    name: "idempotency_key",
    type: "string",
    required: "required",
    description: "Stable key for safe retries and replay-safe billing.",
  },
];

export const sdkRoutingParams: DocsParamRow[] = [
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
    description: "Routing preference when multiple approved services are eligible.",
  },
  {
    name: "provider",
    type: "string",
    required: "optional",
    description: "Restrict the request to one approved provider in the selected provider set.",
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
  {
    name: "features",
    type: "object",
    required: "optional",
    description: "Optional routing hints approved for your account. Most requests can omit this.",
  },
];

export const sdkSupportChecklist = [
  "request_id",
  "job_id, if the request was async",
  "SDK package version",
  "endpoint or SDK method used",
  "file type and approximate size",
  "which expected field looked wrong or missing",
];
