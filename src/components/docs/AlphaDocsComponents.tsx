import { Check, Copy, Info, ShieldAlert } from "lucide-react";
import { ReactNode, useState } from "react";

import type { DocsEndpoint, DocsNavItem, DocsParamRow } from "@/data/alphaApiDocs";

type DocsShellProps = {
  nav: DocsNavItem[];
  children: ReactNode;
};

type DocsCodeBlockProps = {
  title: string;
  language: string;
  code: string;
};

type DocsCalloutProps = {
  tone?: "alpha" | "privacy" | "note";
  title: string;
  children: ReactNode;
};

type DocsParamTableProps = {
  title: string;
  rows: DocsParamRow[];
};

type DocsEndpointProps = {
  endpoint: DocsEndpoint;
};

export const DocsShell = ({ nav, children }: DocsShellProps) => {
  return (
    <div className="max-w-[1400px] mx-auto px-6 md:px-8">
      <div className="md:hidden border border-border bg-secondary/35 p-4 mt-8 mb-2">
        <p className="font-grotesk text-[10px] uppercase tracking-[0.22em] text-muted-foreground mb-4">
          Documentation
        </p>
        <nav aria-label="Mobile documentation navigation">
          <ul className="grid grid-cols-2 gap-2">
            {nav.map((item) => (
              <li key={item.id}>
                <a
                  href={`#${item.id}`}
                  className="block border border-border/70 bg-background/50 px-3 py-2 font-grotesk text-[11px] uppercase tracking-[0.12em] text-muted-foreground hover:text-foreground transition-colors"
                >
                  {item.label}
                </a>
              </li>
            ))}
          </ul>
        </nav>
      </div>

      <div className="grid md:grid-cols-[190px_minmax(0,1fr)] xl:grid-cols-[220px_minmax(0,1fr)_180px] gap-8 xl:gap-14">
        <aside className="hidden md:block">
          <nav
            aria-label="Documentation navigation"
            className="sticky top-28 border border-border bg-secondary/35 px-4 py-5"
          >
            <p className="font-grotesk text-[10px] uppercase tracking-[0.22em] text-muted-foreground mb-2">
              Documentation
            </p>
            <p className="text-[12px] leading-[1.6] text-muted-foreground/75 mb-5">
              Jump to the section you need.
            </p>
            <ul className="space-y-3">
              {nav.map((item, index) => (
                <li key={item.id}>
                  <a
                    href={`#${item.id}`}
                    className="group flex items-center gap-3 text-[13px] leading-none text-muted-foreground hover:text-foreground transition-colors"
                  >
                    <span className="font-mono text-[10px] text-accent/70">
                      {String(index + 1).padStart(2, "0")}
                    </span>
                    <span>{item.label}</span>
                  </a>
                </li>
              ))}
            </ul>
          </nav>
        </aside>

        <main className="min-w-0">{children}</main>

        <aside className="hidden xl:block">
          <div className="sticky top-28 border-l border-border pl-5 py-2">
            <p className="font-grotesk text-[10px] uppercase tracking-[0.22em] text-muted-foreground mb-5">
              On this page
            </p>
            <ul className="space-y-3">
              {nav.slice(0, 6).map((item) => (
                <li key={item.id}>
                  <a
                    href={`#${item.id}`}
                    className="text-[12px] leading-none text-muted-foreground/80 hover:text-accent transition-colors"
                  >
                    {item.label}
                  </a>
                </li>
              ))}
            </ul>
          </div>
        </aside>
      </div>
    </div>
  );
};

export const DocsSection = ({
  id,
  label,
  title,
  children,
}: {
  id: string;
  label?: string;
  title: string;
  children: ReactNode;
}) => {
  return (
    <section id={id} className="scroll-mt-28 border-t border-border py-12 md:py-16">
      {label ? (
        <p className="font-grotesk text-[10px] uppercase tracking-[0.24em] text-accent mb-4">
          {label}
        </p>
      ) : null}
      <h2 className="font-display text-[clamp(2rem,4vw,3rem)] leading-[1.05] tracking-[-0.02em] mb-5">
        {title}
      </h2>
      <div className="space-y-7">{children}</div>
    </section>
  );
};

export const DocsCodeBlock = ({ title, language, code }: DocsCodeBlockProps) => {
  const [copied, setCopied] = useState(false);

  const onCopy = async () => {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(code);
    } else {
      const textarea = document.createElement("textarea");
      textarea.value = code;
      textarea.setAttribute("readonly", "");
      textarea.style.position = "fixed";
      textarea.style.opacity = "0";
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand("copy");
      document.body.removeChild(textarea);
    }
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1400);
  };

  return (
    <div className="border border-[#1e2028] bg-[#0f1117] overflow-hidden">
      <div className="flex items-center justify-between gap-4 border-b border-[#1e2028] bg-[#13141b] px-4 py-3">
        <div>
          <p className="font-grotesk text-[11px] uppercase tracking-[0.18em] text-[#e0e0e6]/40">
            {title}
          </p>
          <p className="font-mono text-[10px] text-[#e0e0e6]/25 mt-1">{language}</p>
        </div>
        <button
          type="button"
          onClick={onCopy}
          className="h-9 w-9 border border-[#2a2b35] text-[#e0e0e6]/50 hover:text-[#e0e0e6] hover:border-[#e0e0e6]/30 transition-colors inline-flex items-center justify-center"
          aria-label={`Copy ${title} example`}
        >
          {copied ? <Check size={15} /> : <Copy size={15} />}
        </button>
      </div>
      <pre className="overflow-x-auto p-5 md:p-6 text-[12px] md:text-[13px] leading-[1.8]">
        <code className="font-mono text-[#d0d0da]">{code}</code>
      </pre>
    </div>
  );
};

export const DocsCallout = ({ tone = "note", title, children }: DocsCalloutProps) => {
  const isAlpha = tone === "alpha";
  const isPrivacy = tone === "privacy";

  return (
    <div className="border border-border bg-secondary/45 px-5 py-4 flex gap-4">
      <div className="mt-0.5 text-accent">
        {isPrivacy || isAlpha ? <ShieldAlert size={18} /> : <Info size={18} />}
      </div>
      <div>
        <p className="font-grotesk text-[12px] uppercase tracking-[0.18em] text-foreground mb-2">
          {title}
        </p>
        <div className="text-sm leading-[1.75] text-muted-foreground">{children}</div>
      </div>
    </div>
  );
};

export const DocsEndpoint = ({ endpoint }: DocsEndpointProps) => {
  return (
    <div className="border-y border-border py-6">
      <div className="flex flex-col md:flex-row md:items-start md:justify-between gap-4 mb-4">
        <div>
          <div className="flex flex-wrap items-center gap-3 mb-3">
            <span className="font-mono text-[11px] tracking-[0.12em] text-accent border border-accent/30 px-2.5 py-1">
              {endpoint.method}
            </span>
            <code className="font-mono text-[13px] md:text-sm text-foreground break-all">
              {endpoint.path}
            </code>
          </div>
          <h3 className="font-grotesk text-lg md:text-xl font-semibold tracking-[-0.02em]">
            {endpoint.title}
          </h3>
        </div>
      </div>
      <p className="text-sm md:text-[15px] text-muted-foreground leading-[1.8] mb-4">
        {endpoint.description}
      </p>
      <ul className="grid gap-2">
        {endpoint.notes.map((note) => (
          <li key={note} className="flex gap-3 text-sm text-muted-foreground leading-[1.6]">
            <span className="mt-[0.65em] h-px w-4 bg-accent/50 flex-shrink-0" />
            <span>{note}</span>
          </li>
        ))}
      </ul>
    </div>
  );
};

export const DocsParamTable = ({ title, rows }: DocsParamTableProps) => {
  return (
    <div>
      <h3 className="font-grotesk text-[13px] uppercase tracking-[0.18em] text-muted-foreground mb-4">
        {title}
      </h3>
      <div className="border border-border overflow-x-auto">
        <table className="w-full min-w-[680px] text-left">
          <thead className="bg-secondary/60 border-b border-border">
            <tr>
              {["Name", "Type", "Required", "Description"].map((heading) => (
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
            {rows.map((row) => (
              <tr key={row.name} className="border-b border-border last:border-b-0">
                <td className="px-4 py-4 align-top">
                  <code className="font-mono text-[12px] text-foreground">{row.name}</code>
                </td>
                <td className="px-4 py-4 align-top font-mono text-[12px] text-muted-foreground">
                  {row.type}
                </td>
                <td className="px-4 py-4 align-top font-mono text-[12px] text-muted-foreground">
                  {row.required}
                </td>
                <td className="px-4 py-4 align-top text-sm text-muted-foreground leading-[1.6]">
                  {row.description}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
};
