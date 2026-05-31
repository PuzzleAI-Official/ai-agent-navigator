import Footer from "@/components/landing/Footer";
import Navbar from "@/components/landing/Navbar";
import {
  DocsCallout,
  DocsCodeBlock,
  DocsParamTable,
  DocsSection,
  DocsShell,
} from "@/components/docs/AlphaDocsComponents";
import {
  alphaSdkDocsNav,
  sdkClientParams,
  sdkCodeExamples,
  sdkDocumentParams,
  sdkInvoiceParams,
  sdkQuickstartSteps,
  sdkRoutingParams,
  sdkSupportChecklist,
} from "@/data/alphaSdkDocs";

const AlphaSdkDocs = () => {
  return (
    <div className="min-h-screen bg-background">
      <Navbar />

      <section className="relative overflow-hidden border-b border-border">
        <div className="absolute inset-0 topo-lines opacity-70" />
        <div className="relative max-w-[1400px] mx-auto px-6 md:px-8 pt-36 md:pt-44 pb-16 md:pb-20">
          <div className="max-w-[840px]">
            <div className="flex items-center gap-4 mb-8">
              <div
                className="w-8 h-[2px] bg-accent/30"
                style={{ transform: "skewX(-20deg)" }}
              />
              <span className="font-grotesk font-semibold text-[11px] uppercase tracking-[0.25em] text-muted-foreground">
                Client library
              </span>
            </div>

            <h1 className="font-display text-[clamp(3rem,7vw,6.5rem)] leading-[0.9] tracking-[-0.03em] text-foreground mb-8">
              Puzzle Python SDK
            </h1>
            <p className="text-[16px] md:text-[18px] text-muted-foreground max-w-[720px] leading-[1.85]">
              Install <code className="font-mono text-foreground">puzzleai</code>,
              upload documents, run invoice workflows, poll async jobs, and handle
              Puzzle errors from Python.
            </p>
            <div className="flex flex-wrap gap-3 mt-8">
              <a
                href="/alpha-api"
                className="inline-flex items-center justify-center border border-border bg-secondary/45 px-5 py-3 font-grotesk text-[12px] uppercase tracking-[0.16em] text-foreground hover:border-accent hover:text-accent transition-colors"
              >
                API reference
              </a>
              <a
                href="#quickstart"
                className="inline-flex items-center justify-center border border-foreground bg-foreground px-5 py-3 font-grotesk text-[12px] uppercase tracking-[0.16em] text-background hover:bg-accent hover:border-accent transition-colors"
              >
                Start quickstart
              </a>
            </div>
          </div>
        </div>
      </section>

      <DocsShell nav={alphaSdkDocsNav}>
        <DocsSection id="quickstart" label="Start here" title="Quickstart">
          <DocsCallout tone="alpha" title="Alpha access">
            Use the API key, base URL, and workflows enabled for your alpha
            account. The package name and import name are both
            <code className="font-mono text-foreground mx-1">puzzleai</code>.
          </DocsCallout>

          <ol className="grid gap-3">
            {sdkQuickstartSteps.map((step, index) => (
              <li key={step} className="flex gap-4 text-muted-foreground leading-[1.75]">
                <span className="font-mono text-[12px] text-accent pt-1">
                  {String(index + 1).padStart(2, "0")}
                </span>
                <span>{step}</span>
              </li>
            ))}
          </ol>

          <DocsCodeBlock {...sdkCodeExamples.install} />
          <DocsCodeBlock {...sdkCodeExamples.syncInvoice} />
        </DocsSection>

        <DocsSection id="client" label="Setup" title="Client setup">
          <p className="text-muted-foreground leading-[1.85] max-w-[760px]">
            Create a client once and reuse it for requests. You can pass the
            alpha base URL directly or set
            <code className="font-mono text-foreground mx-1">PUZZLE_BASE_URL</code>
            in your environment.
          </p>
          <DocsParamTable title="Client constructor" rows={sdkClientParams} />
          <DocsCodeBlock {...sdkCodeExamples.envClient} />
          <DocsCodeBlock {...sdkCodeExamples.contextManager} />
        </DocsSection>

        <DocsSection id="invoices" label="Workflow" title="Invoice extraction">
          <p className="text-muted-foreground leading-[1.85] max-w-[760px]">
            Use invoice extraction when you want normalized invoice fields such
            as vendor, dates, totals, tax, and line items. Every billable request
            should include an idempotency key.
          </p>
          <DocsParamTable title="Invoice method options" rows={sdkInvoiceParams} />
          <DocsCodeBlock {...sdkCodeExamples.invoiceOptions} />
        </DocsSection>

        <DocsSection id="documents" label="Lower-level API" title="Document processing">
          <p className="text-muted-foreground leading-[1.85] max-w-[760px]">
            Use general document processing when you need normalized text, fields,
            or tables without invoice-specific output rules.
          </p>
          <DocsParamTable title="Document method options" rows={sdkDocumentParams} />
          <DocsCodeBlock {...sdkCodeExamples.documentProcess} />
        </DocsSection>

        <DocsSection id="async" label="Jobs" title="Async usage">
          <p className="text-muted-foreground leading-[1.85] max-w-[760px]">
            Submit async jobs when work should run in the background. Poll
            <code className="font-mono text-foreground mx-1">client.jobs.get()</code>
            with the returned job ID.
          </p>
          <DocsCodeBlock {...sdkCodeExamples.asyncSubmit} />
          <DocsCodeBlock {...sdkCodeExamples.asyncClient} />
        </DocsSection>

        <DocsSection id="routing" label="Provider choice" title="Routing options">
          <p className="text-muted-foreground leading-[1.85] max-w-[760px]">
            Most requests can omit routing fields and let Puzzle select from your
            approved provider set. Use explicit routing only when your account is
            configured for it or when Puzzle support asks you to test a route.
          </p>
          <DocsParamTable title="Optional routing fields" rows={sdkRoutingParams} />
          <DocsCallout title="Provider allowlists">
            Provider sets are configured for your account during alpha. Puzzle
            only routes to providers enabled for the requested workflow.
          </DocsCallout>
        </DocsSection>

        <DocsSection id="errors" label="Failures" title="Error handling">
          <p className="text-muted-foreground leading-[1.85] max-w-[760px]">
            Puzzle maps API errors to Python exceptions. Every
            <code className="font-mono text-foreground mx-1">PuzzleError</code>
            includes a code, request ID when available, and details.
          </p>
          <DocsCodeBlock {...sdkCodeExamples.errors} />
        </DocsSection>

        <DocsSection id="support" label="Debugging" title="Support information">
          <p className="text-muted-foreground leading-[1.85] max-w-[760px]">
            Include the fields below when reporting an issue so Puzzle can trace
            the request without needing secrets or raw document data.
          </p>
          <div className="grid md:grid-cols-2 gap-3">
            {sdkSupportChecklist.map((item) => (
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

export default AlphaSdkDocs;
