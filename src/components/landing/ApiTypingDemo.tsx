import { useState, useEffect, useRef } from "react";

const CODE_LINES = [
  { text: "# When your agent needs to hire another agent.", type: "comment" },
  { text: "# This is what that looks like.", type: "comment" },
  { text: "", type: "blank" },
  { text: "from puzzleai import AgentHR", type: "import" },
  { text: "", type: "blank" },
  { text: "# Your orchestrator agent describes what it needs", type: "comment" },
  { text: 'hr = AgentHR(protocol="a2a")', type: "code" },
  { text: "", type: "blank" },
  { text: "# 1. Scout — discover agents via A2A Agent Cards", type: "comment" },
  { text: "candidates = hr.discover(", type: "code" },
  { text: '    role="Translate customer tickets from JP → EN,', type: "string" },
  { text: '          then route by urgency",', type: "string" },
  { text: '    requirements=["multilingual", "sub-2s latency", "pii-safe"],', type: "code" },
  { text: '    budget="$0.02/ticket"', type: "string" },
  { text: ") # → 12 agents matched from 3 registries", type: "code-comment" },
  { text: "", type: "blank" },
  { text: "# 2. Interview — sandbox each candidate with your data", type: "comment" },
  { text: "results = hr.evaluate(", type: "code" },
  { text: "    candidates=candidates,", type: "code" },
  { text: '    test_data="s3://our-tickets/sample_500.jsonl",', type: "string" },
  { text: '    criteria=["accuracy", "latency", "cost", "pii_leakage"],', type: "code" },
  { text: "    sandbox=True # isolated environment, your data never leaves", type: "code-comment" },
  { text: ")", type: "code" },
  { text: "", type: "blank" },
  { text: "# 3. Hire — the best candidate joins your workflow", type: "comment" },
  { text: 'hired = hr.select(results, strategy="pareto-optimal")', type: "code" },
  { text: 'hired.onboard(webhook="https://ops.acme.com/agents/new")', type: "code" },
  { text: "", type: "blank" },
  { text: "# 4. Monitor — continuous performance reviews", type: "comment" },
  { text: "hr.monitor(", type: "code" },
  { text: "    agent=hired,", type: "code" },
  { text: '    sla={"accuracy": 0.95, "latency_p99": "1800ms"},', type: "code" },
  { text: '    on_underperform="re-evaluate" # automatically find a replacement', type: "code-comment" },
  { text: ")", type: "code" },
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
        <span className="text-muted-foreground/45">{visible}</span>
      );
    }

    if (type === "import") {
      // Highlight "from" and "import" keywords
      return highlightPython(visible);
    }

    if (type === "string") {
      return <span className="text-amber-700/80">{visible}</span>;
    }

    if (type === "code-comment") {
      // Split at #
      const hashIdx = text.indexOf(" #");
      if (hashIdx >= 0 && charsToShow > hashIdx) {
        return (
          <>
            {highlightPython(visible.slice(0, hashIdx))}
            <span className="text-muted-foreground/45">{visible.slice(hashIdx)}</span>
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
            <span key={i} className="text-violet-600/80 font-semibold">{p.text}</span>
          ) : p.isString ? (
            <span key={i} className="text-amber-700/80">{p.text}</span>
          ) : (
            <span key={i} className="text-foreground/85">{p.text}</span>
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
        {showCursor && <span className="animate-pulse text-foreground/60">▊</span>}
      </div>
    );
  });

  const isComplete = visibleChars >= totalChars;
  let lineNum = 0;

  return (
    <div ref={ref}>
      {/* A2A message */}
      <p className="text-[15px] md:text-[17px] text-foreground/90 leading-[1.8] max-w-[640px] mb-8">
        A2A-compatible. Agent Card discovery, sandbox-as-a-service, and evaluation 
        endpoints — designed for a world where agents choose their own tools.
      </p>

      {/* Editor header */}
      <div className="border border-border border-b-0 px-4 py-3 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="flex gap-1.5">
            <div className="w-2.5 h-2.5 rounded-full bg-muted-foreground/20" />
            <div className="w-2.5 h-2.5 rounded-full bg-muted-foreground/20" />
            <div className="w-2.5 h-2.5 rounded-full bg-muted-foreground/20" />
          </div>
          <span className="font-mono text-[11px] text-muted-foreground/50 ml-2">
            hire_agent.py
          </span>
        </div>
        <span className="font-mono text-[10px] text-muted-foreground/30 uppercase tracking-wider">
          {isComplete ? "ready" : "typing…"}
        </span>
      </div>

      {/* Code area */}
      <div className="border border-border bg-foreground/[0.02] p-6 md:p-8 font-mono text-[12px] md:text-[13px] leading-[1.85] overflow-x-auto">
        <div className="flex">
          {/* Line numbers */}
          <div className="select-none pr-6 text-muted-foreground/20 text-right min-w-[32px]">
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
      <div className="border border-border border-t-0 px-4 py-2.5 flex items-center justify-between">
        <span className="font-mono text-[10px] text-muted-foreground/40">
          Python 3.12 · A2A Protocol
        </span>
        <span className="font-mono text-[10px] text-muted-foreground/30">
          UTF-8 · LF
        </span>
      </div>

      {/* Footnote */}
      <p className="mt-8 text-[13px] text-muted-foreground/50 leading-[1.8] max-w-[640px]">
        This is where we're headed. The API above is illustrative — we're building it now 
        and opening early access later this year. If you want to shape how agents hire agents, 
        get on the list.
      </p>
    </div>
  );
};

export default ApiTypingDemo;
