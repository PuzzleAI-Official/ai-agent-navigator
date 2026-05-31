import { useState, useEffect, useRef } from "react";

const CODE_LINES = [
  { text: "# Hand Puzzle a document. Get back normalized, structured data.", type: "comment" },
  { text: "# One API, many verified provider services behind it.", type: "comment" },
  { text: "", type: "blank" },
  { text: "from puzzleai import Client", type: "import" },
  { text: "", type: "blank" },
  { text: "# Authenticate with your alpha API key and base URL", type: "comment" },
  { text: "client = Client(", type: "code" },
  { text: '    api_key="YOUR_API_KEY",', type: "code" },
  { text: '    base_url="YOUR_ALPHA_BASE_URL",', type: "code" },
  { text: ")", type: "code" },
  { text: "", type: "blank" },
  { text: "# 1. Extract — upload an invoice, receive normalized fields", type: "comment" },
  { text: 'with open("invoice.pdf", "rb") as file:', type: "code" },
  { text: "    result = client.documents.invoices.extract(", type: "code" },
  { text: "        file=file,", type: "code" },
  { text: '        line_items_mode="preferred",', type: "code" },
  { text: '        required_fields=["total"],', type: "code" },
  { text: '        idempotency_key="invoice-demo-001", # replay-safe billing', type: "code-comment" },
  { text: "    )", type: "code" },
  { text: "", type: "blank" },
  { text: "# Every field comes back with a confidence score", type: "comment" },
  { text: 'print(result["request_id"])', type: "code" },
  { text: '_total = result["invoice"]["total"]', type: "code" },
  { text: 'print(_total["value"], _total["confidence"]) # → 108.25 0.99', type: "code-comment" },
  { text: "", type: "blank" },
  { text: "# 2. Route — pin a provider set or strategy when you need control", type: "comment" },
  { text: 'with open("invoice.pdf", "rb") as file:', type: "code" },
  { text: "    routed = client.documents.invoices.extract(", type: "code" },
  { text: "        file=file,", type: "code" },
  { text: '        provider_set="YOUR_PROVIDER_SET",', type: "code" },
  { text: '        strategy="highest_quality",', type: "code" },
  { text: '        idempotency_key="invoice-route-001",', type: "code" },
  { text: "    )", type: "code" },
  { text: "", type: "blank" },
  { text: "# 3. Scale — submit async jobs and poll until they finish", type: "comment" },
  { text: 'with open("invoice.pdf", "rb") as file:', type: "code" },
  { text: "    job = client.documents.invoices.submit(", type: "code" },
  { text: "        file=file,", type: "code" },
  { text: '        idempotency_key="invoice-async-001",', type: "code" },
  { text: "    )", type: "code" },
  { text: "", type: "blank" },
  { text: 'status = client.jobs.get(job["job_id"]) # → succeeded | failed', type: "code-comment" },
];

const ApiTypingDemo = () => {
  const [visibleChars, setVisibleChars] = useState(0);
  const [isInView, setIsInView] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  const lineTexts = CODE_LINES.map((l) => l.text);
  const totalChars = lineTexts.reduce((sum, l) => sum + l.length + 1, 0);

  useEffect(() => {
    const observer = new IntersectionObserver(
      ([entry]) => { if (entry.isIntersecting) setIsInView(true); },
      { threshold: 0.2 }
    );
    if (ref.current) observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!isInView || visibleChars >= totalChars) return;
    const speed = Math.random() * 18 + 8;
    const timer = setTimeout(() => setVisibleChars((c) => c + 1), speed);
    return () => clearTimeout(timer);
  }, [isInView, visibleChars, totalChars]);

  let charCount = 0;

  const renderLine = (text: string, type: string, charsToShow: number, showCursor: boolean) => {
    const visible = text.slice(0, charsToShow);

    if (type === "comment") {
      return (
        <span className="text-[#e0e0e6]/35">{visible}</span>
      );
    }

    if (type === "import") {
      // Highlight "from" and "import" keywords
      return highlightPython(visible);
    }

    if (type === "string") {
      return <span className="text-[#c9a96e]">{visible}</span>;
    }

    if (type === "code-comment") {
      // Split at #
      const hashIdx = text.indexOf(" #");
      if (hashIdx >= 0 && charsToShow > hashIdx) {
        return (
          <>
            {highlightPython(visible.slice(0, hashIdx))}
            <span className="text-[#e0e0e6]/35">{visible.slice(hashIdx)}</span>
          </>
        );
      }
      return highlightPython(visible);
    }

    return highlightPython(visible);
  };

  const highlightPython = (text: string) => {
    // Simple keyword highlighting
    const keywords = ["from", "import", "True", "False", "None"];
    const parts: { text: string; isKeyword: boolean; isString: boolean }[] = [];

    let remaining = text;
    while (remaining.length > 0) {
      // Check for string literals
      const strMatch = remaining.match(/^("(?:[^"\\]|\\.)*"?)/);
      if (strMatch) {
        parts.push({ text: strMatch[1], isKeyword: false, isString: true });
        remaining = remaining.slice(strMatch[1].length);
        continue;
      }

      // Check for keywords
      let foundKeyword = false;
      for (const kw of keywords) {
        if (remaining.startsWith(kw) && (remaining.length === kw.length || /[^a-zA-Z_]/.test(remaining[kw.length]))) {
          parts.push({ text: kw, isKeyword: true, isString: false });
          remaining = remaining.slice(kw.length);
          foundKeyword = true;
          break;
        }
      }
      if (foundKeyword) continue;

      // Regular char
      const nextSpecial = remaining.slice(1).search(/("|(?:^|\b)(?:from|import|True|False|None)(?:\b|$))/);
      const chunk = nextSpecial >= 0 ? remaining.slice(0, nextSpecial + 1) : remaining;
      parts.push({ text: chunk, isKeyword: false, isString: false });
      remaining = remaining.slice(chunk.length);
    }

    return (
      <>
        {parts.map((p, i) =>
          p.isKeyword ? (
            <span key={i} className="text-[hsl(215,25%,65%)] font-semibold">{p.text}</span>
          ) : p.isString ? (
            <span key={i} className="text-[#c9a96e]">{p.text}</span>
          ) : (
            <span key={i} className="text-[#d0d0da]">{p.text}</span>
          )
        )}
      </>
    );
  };

  const renderedLines = CODE_LINES.map((line, i) => {
    const lineText = lineTexts[i];
    const lineStart = charCount;
    charCount += lineText.length + 1;
    const charsToShow = Math.max(0, Math.min(lineText.length, visibleChars - lineStart));

    if (charsToShow <= 0 && visibleChars < totalChars) return null;
    if (line.type === "blank") return <div key={i} className="h-[1.6em]" />;

    const showCursor = visibleChars >= lineStart && visibleChars < lineStart + lineText.length + 1;

    return (
      <div key={i} className="whitespace-pre">
        {renderLine(lineText, line.type, charsToShow, showCursor)}
        {showCursor && <span className="animate-pulse text-[hsl(215,25%,65%)]">▊</span>}
      </div>
    );
  });

  const isComplete = visibleChars >= totalChars;
  let lineNum = 0;

  return (
    <div ref={ref}>
      {/* Editor header */}
      <div className="border border-[#1e2028] border-b-0 px-4 py-3 flex items-center justify-between bg-[#13141b]">
        <div className="flex items-center gap-3">
          <div className="flex gap-1.5">
            <div className="w-2.5 h-2.5 rounded-full bg-[#2a2b35]" />
            <div className="w-2.5 h-2.5 rounded-full bg-[#2a2b35]" />
            <div className="w-2.5 h-2.5 rounded-full bg-[#2a2b35]" />
          </div>
          <span className="font-mono text-[11px] text-[#e0e0e6]/30 ml-2">
            PUZZLE ALPHA SDK - PREVIEW
          </span>
        </div>
        <span className="font-mono text-[10px] text-[#e0e0e6]/20 uppercase tracking-wider">
          {isComplete ? "ready" : "typing…"}
        </span>
      </div>

      {/* Code area */}
      <div className="border border-[#1e2028] bg-[#0f1117] p-6 md:p-8 font-mono text-[12px] md:text-[13px] leading-[1.85] overflow-x-auto">
        <div className="flex">
          {/* Line numbers */}
          <div className="select-none pr-6 text-[#e0e0e6]/15 text-right min-w-[32px]">
            {CODE_LINES.map((line, i) => {
              const lineStart2 = lineTexts.slice(0, i).reduce((s, l) => s + l.length + 1, 0);
              if (visibleChars < lineStart2 && visibleChars < totalChars) return null;
              lineNum++;
              return (
                <div key={i} className={line.type === "blank" ? "h-[1.6em]" : ""}>
                  {line.type !== "blank" ? lineNum : ""}
                </div>
              );
            })}
          </div>
          {/* Code */}
          <div className="flex-1">{renderedLines}</div>
        </div>
      </div>

      {/* Footer */}
      <div className="border border-[#1e2028] border-t-0 px-4 py-2.5 flex items-center justify-between bg-[#13141b]">
        <span className="font-mono text-[10px] text-[#e0e0e6]/25">
          Python 3.12 · Puzzle Alpha API
        </span>
        <span className="font-mono text-[10px] text-[#e0e0e6]/15">
          UTF-8 · LF
        </span>
      </div>

    </div>
  );
};

export default ApiTypingDemo;
