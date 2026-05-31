import Footer from "@/components/landing/Footer";
import Navbar from "@/components/landing/Navbar";
import {
  DocsCallout,
  DocsCodeBlock,
  DocsEndpoint,
  DocsParamTable,
  DocsSection,
  DocsShell,
} from "@/components/docs/AlphaDocsComponents";
import {
  alphaDocsNav,
  codeExamples,
  endpoints,
  errorCodes,
  headerParams,
  invoiceMetadataParams,
  multipartParams,
  quickstartSteps,
  responseFields,
  routingControlParams,
  supportChecklist,
} from "@/data/alphaApiDocs";

const AlphaApiDocs = () => {
  return (
    <div className="min-h-screen bg-background">
      <Navbar />

      <section className="relative overflow-hidden border-b border-border">
        <div className="absolute inset-0 topo-lines opacity-70" />
        <div className="relative max-w-[1400px] mx-auto px-6 md:px-8 pt-36 md:pt-44 pb-16 md:pb-20">
          <div className="max-w-[820px]">
            <div className="flex items-center gap-4 mb-8">
              <div
                className="w-8 h-[2px] bg-accent/30"
                style={{ transform: "skewX(-20deg)" }}
              />
              <span className="font-grotesk font-semibold text-[11px] uppercase tracking-[0.25em] text-muted-foreground">
                Alpha documentation
              </span>
            </div>

            <h1 className="font-display text-[clamp(3rem,7vw,6.5rem)] leading-[0.9] tracking-[-0.03em] text-foreground mb-8">
              Puzzle Alpha API
            </h1>
            <p className="text-[16px] md:text-[18px] text-muted-foreground max-w-[720px] leading-[1.85]">
              Route document and invoice workflows through verified provider services,
              receive normalized results, and keep retries safe with idempotency.
            </p>
            <div className="flex flex-wrap gap-3 mt-8">
              <a
                href="/alpha-sdk"
                className="inline-flex items-center justify-center border border-foreground bg-foreground px-5 py-3 font-grotesk text-[12px] uppercase tracking-[0.16em] text-background hover:bg-accent hover:border-accent transition-colors"
              >
                Python SDK docs
              </a>
              <a
                href="#endpoints"
                className="inline-flex items-center justify-center border border-border bg-secondary/45 px-5 py-3 font-grotesk text-[12px] uppercase tracking-[0.16em] text-foreground hover:border-accent hover:text-accent transition-colors"
              >
                API reference
              </a>
            </div>
          </div>
        </div>
      </section>

      <DocsShell nav={alphaDocsNav}>
        <DocsSection id="quickstart" label="Start here" title="Quickstart">
          <DocsCallout tone="alpha" title="Alpha access">
            This API is available to approved alpha customers. Use the base URL,
            API key, and workflows Puzzle enables for your account. We coordinate
            API updates directly with alpha teams.
          </DocsCallout>

          <ol className="grid gap-3">
            {quickstartSteps.map((step, index) => (
              <li key={step} className="flex gap-4 text-muted-foreground leading-[1.75]">
                <span className="font-mono text-[12px] text-accent pt-1">
                  {String(index + 1).padStart(2, "0")}
                </span>
                <span>{step}</span>
              </li>
            ))}
          </ol>

          <DocsCodeBlock {...codeExamples.syncInvoice} />
        </DocsSection>

        <DocsSection id="python-sdk" label="Client library" title="Python SDK">
          <p className="text-muted-foreground leading-[1.85] max-w-[760px]">
            Prefer Python? The SDK has its own guide with installation, client
            setup, invoice extraction, document processing, async jobs, routing
            options, and error handling examples.
          </p>

          <div className="border border-border bg-secondary/35 px-5 py-5">
            <div className="flex flex-col md:flex-row md:items-center md:justify-between gap-5">
              <div>
                <p className="font-grotesk text-[12px] uppercase tracking-[0.18em] text-foreground mb-2">
                  Python package
                </p>
                <code className="font-mono text-sm text-muted-foreground">
                  pip install puzzleai
                </code>
              </div>
              <a
                href="/alpha-sdk"
                className="inline-flex items-center justify-center border border-foreground px-5 py-3 font-grotesk text-[12px] uppercase tracking-[0.16em] text-foreground hover:bg-foreground hover:text-background transition-colors"
              >
                Open SDK docs
              </a>
            </div>
          </div>
        </DocsSection>

        <DocsSection id="authentication" label="Access" title="Authentication">
          <p className="text-muted-foreground leading-[1.85] max-w-[760px]">
            Send your alpha API key with every request. Prefer the Authorization
            header when possible.
          </p>
          <DocsParamTable title="Headers" rows={headerParams} />
          <DocsCallout tone="privacy" title="Keep keys private">
            Never paste API keys, provider credentials, raw secrets, or unrelated
            private data into support requests. Puzzle support only needs request
            identifiers and high-level document context.
          </DocsCallout>
        </DocsSection>

        <DocsSection id="idempotency" label="Safe retries" title="Idempotency">
          <p className="text-muted-foreground leading-[1.85] max-w-[760px]">
            Billable requests require an idempotency key. If your network retries
            the same request with the same key and same content, Puzzle returns the
            stored result and does not create another billable attempt.
          </p>
          <DocsCallout title="Use one key per operation">
            Reusing a key with different file content or different metadata can
            return an idempotency conflict. Generate a new key for each new document
            operation.
          </DocsCallout>
        </DocsSection>

        <DocsSection id="endpoints" label="Reference" title="Endpoints">
          <div className="space-y-6">
            {endpoints.map((endpoint) => (
              <DocsEndpoint key={`${endpoint.method}-${endpoint.path}`} endpoint={endpoint} />
            ))}
          </div>

          <DocsParamTable title="Multipart form fields" rows={multipartParams} />
          <DocsCodeBlock {...codeExamples.asyncInvoice} />
        </DocsSection>

        <DocsSection id="routing-controls" label="Provider choice" title="Routing controls">
          <p className="text-muted-foreground leading-[1.85] max-w-[760px]">
            Most requests should let Puzzle select the best verified provider
            service. If you need more control, choose a provider set, a routing
            strategy, or an explicit provider route in request metadata.
          </p>

          <div className="grid md:grid-cols-3 gap-3">
            <div className="border border-border px-4 py-4">
              <p className="font-grotesk text-[12px] uppercase tracking-[0.18em] text-foreground mb-2">
                Automatic
              </p>
              <p className="text-sm leading-[1.7] text-muted-foreground">
                Omit provider fields and let Puzzle choose from verified services
                in your default provider set.
              </p>
            </div>
            <div className="border border-border px-4 py-4">
              <p className="font-grotesk text-[12px] uppercase tracking-[0.18em] text-foreground mb-2">
                Allowlist
              </p>
              <p className="text-sm leading-[1.7] text-muted-foreground">
                Use a named provider set when your account has multiple approved
                routing pools.
              </p>
            </div>
            <div className="border border-border px-4 py-4">
              <p className="font-grotesk text-[12px] uppercase tracking-[0.18em] text-foreground mb-2">
                Explicit
              </p>
              <p className="text-sm leading-[1.7] text-muted-foreground">
                Set provider and service_id when you want a controlled provider
                test or customer-directed route.
              </p>
            </div>
          </div>

          <DocsParamTable title="Routing metadata fields" rows={routingControlParams} />

          <DocsCallout title="Provider allowlists">
            During alpha, provider allowlists are configured as account-level
            provider sets. Puzzle only routes to providers that are enabled,
            credentialed, and verified for the requested workflow.
          </DocsCallout>

          <DocsCodeBlock {...codeExamples.routingControls} />
          <DocsCodeBlock {...codeExamples.explicitProvider} />
        </DocsSection>

        <DocsSection id="invoice-metadata" label="Workflow options" title="Invoice metadata">
          <p className="text-muted-foreground leading-[1.85] max-w-[760px]">
            The invoice endpoints accept a JSON string in the multipart
            <code className="font-mono text-foreground mx-1">metadata</code>
            field. Omit fields you do not need; defaults are chosen for alpha
            invoice extraction.
          </p>
          <DocsParamTable title="Metadata fields" rows={invoiceMetadataParams} />
        </DocsSection>

        <DocsSection id="responses" label="Output" title="Responses">
          <p className="text-muted-foreground leading-[1.85] max-w-[760px]">
            Puzzle returns normalized workflow results. Provider-specific raw
            payloads are not returned in public API responses.
          </p>
          <DocsParamTable title="Common response fields" rows={responseFields} />
          <DocsCodeBlock {...codeExamples.response} />
        </DocsSection>

        <DocsSection id="errors" label="Failures" title="Error codes">
          <p className="text-muted-foreground leading-[1.85] max-w-[760px]">
            Error responses include a stable code, human-readable message, and
            request identifier when available.
          </p>
          <div className="border border-border overflow-x-auto">
            <table className="w-full min-w-[760px] text-left">
              <thead className="bg-secondary/60 border-b border-border">
                <tr>
                  {["Code", "Meaning", "Action"].map((heading) => (
                    <th
                      key={heading}
                      className="font-grotesk text-[10px] uppercase tracking-[0.18em] text-muted-foreground px-4 py-3"
                    >
                      {heading}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {errorCodes.map((error) => (
                  <tr key={error.code} className="border-b border-border last:border-b-0">
                    <td className="px-4 py-4 align-top">
                      <code className="font-mono text-[12px] text-foreground">
                        {error.code}
                      </code>
                    </td>
                    <td className="px-4 py-4 align-top text-sm text-muted-foreground leading-[1.6]">
                      {error.meaning}
                    </td>
                    <td className="px-4 py-4 align-top text-sm text-muted-foreground leading-[1.6]">
                      {error.action}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <DocsCodeBlock {...codeExamples.error} />
        </DocsSection>

        <DocsSection id="support" label="Debugging" title="Support information">
          <p className="text-muted-foreground leading-[1.85] max-w-[760px]">
            When reporting an issue, include the fields below so Puzzle can trace
            the request without needing secrets or raw credentials.
          </p>
          <div className="grid md:grid-cols-2 gap-3">
            {supportChecklist.map((item) => (
              <div key={item} className="border border-border px-4 py-3">
                <span className="font-mono text-[12px] text-muted-foreground">{item}</span>
              </div>
            ))}
          </div>
          <DocsCallout tone="privacy" title="Do not send secrets">
            Do not send API keys, provider credentials, raw secrets, or unrelated
            private data in support messages.
          </DocsCallout>
        </DocsSection>
      </DocsShell>

      <Footer />
    </div>
  );
};

export default AlphaApiDocs;
