import { useState, useEffect, useRef } from "react";

const API_LINES = [
  { type: "comment", text: "# Agent Card Discovery" },
  { type: "method", method: "GET", path: "/v1/agents", desc: "List all available agents" },
  { type: "method", method: "GET", path: "/v1/agents/{id}/card", desc: "Retrieve agent capability card" },
  { type: "method", method: "POST", path: "/v1/agents/discover", desc: "Search agents by task description" },
  { type: "blank" },
  { type: "comment", text: "# Sandbox-as-a-Service" },
  { type: "method", method: "POST", path: "/v1/sandbox/create", desc: "Spin up isolated test environment" },
  { type: "method", method: "POST", path: "/v1/sandbox/{id}/run", desc: "Execute workflow in sandbox" },
  { type: "method", method: "DELETE", path: "/v1/sandbox/{id}", desc: "Tear down sandbox instance" },
  { type: "blank" },
  { type: "comment", text: "# Evaluation Endpoints" },
  { type: "method", method: "POST", path: "/v1/evaluate", desc: "Run head-to-head benchmark" },
  { type: "method", method: "GET", path: "/v1/evaluate/{id}/results", desc: "Get performance verdicts" },
  { type: "method", method: "GET", path: "/v1/evaluate/{id}/compare", desc: "Side-by-side comparison matrix" },
];

const METHOD_COLORS: Record<string, string> = {
  GET: "text-emerald-600",
  POST: "text-amber-600",
  DELETE: "text-red-500",
};

const ApiTypingDemo = () => {
  const [visibleChars, setVisibleChars] = useState(0);
  const [isInView, setIsInView] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  // Build full text for character counting
  const fullLines = API_LINES.map((line) => {
    if (line.type === "blank") return "";
    if (line.type === "comment") return line.text!;
    return `${line.method}  ${line.path}  ${line.desc}`;
  });
  const totalChars = fullLines.reduce((sum, l) => sum + l.length + 1, 0);

  useEffect(() => {
    const observer = new IntersectionObserver(
      ([entry]) => { if (entry.isIntersecting) setIsInView(true); },
      { threshold: 0.3 }
    );
    if (ref.current) observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!isInView) return;
    if (visibleChars >= totalChars) return;
    const speed = Math.random() * 20 + 10;
    const timer = setTimeout(() => setVisibleChars((c) => c + 1), speed);
    return () => clearTimeout(timer);
  }, [isInView, visibleChars, totalChars]);

  // Render lines with typing effect
  let charCount = 0;
  const renderedLines = API_LINES.map((line, i) => {
    const lineText = fullLines[i];
    const lineStart = charCount;
    charCount += lineText.length + 1;
    const charsToShow = Math.max(0, Math.min(lineText.length, visibleChars - lineStart));

    if (charsToShow <= 0 && visibleChars < totalChars) return null;
    if (line.type === "blank") return <div key={i} className="h-4" />;

    const visibleText = lineText.slice(0, charsToShow);
    const showCursor = visibleChars >= lineStart && visibleChars < lineStart + lineText.length + 1;

    if (line.type === "comment") {
      return (
        <div key={i} className="text-muted-foreground/50 select-none">
          {visibleText}
          {showCursor && <span className="animate-pulse">▊</span>}
        </div>
      );
    }

    const methodEnd = line.method!.length;
    const pathStart = methodEnd + 2;
    const pathEnd = pathStart + line.path!.length;
    const descStart = pathEnd + 2;

    return (
      <div key={i} className="flex gap-0 whitespace-nowrap">
        <span className={`${METHOD_COLORS[line.method!]} font-bold min-w-[70px] inline-block`}>
          {visibleText.slice(0, methodEnd)}
        </span>
        {charsToShow > pathStart && (
          <span className="text-foreground/90">
            {visibleText.slice(pathStart, Math.min(charsToShow, pathEnd))}
          </span>
        )}
        {charsToShow > descStart && (
          <span className="text-muted-foreground/50 ml-4">
            {"// "}{visibleText.slice(descStart)}
          </span>
        )}
        {showCursor && <span className="animate-pulse text-foreground/70">▊</span>}
      </div>
    );
  });

  const isComplete = visibleChars >= totalChars;

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
            api-reference.rest
          </span>
        </div>
        <span className="font-mono text-[10px] text-muted-foreground/30 uppercase tracking-wider">
          {isComplete ? "ready" : "typing…"}
        </span>
      </div>

      {/* Code area */}
      <div className="border border-border bg-foreground/[0.02] p-6 md:p-8 font-mono text-[12px] md:text-[13px] leading-[2] overflow-x-auto min-h-[340px]">
        <div className="flex">
          {/* Line numbers */}
          <div className="select-none pr-6 text-muted-foreground/25 text-right min-w-[32px]">
            {API_LINES.map((_, i) => {
              const lineStart = fullLines.slice(0, i).reduce((s, l) => s + l.length + 1, 0);
              if (visibleChars < lineStart && visibleChars < totalChars) return null;
              return <div key={i} className={API_LINES[i].type === "blank" ? "h-4" : ""}>{API_LINES[i].type !== "blank" ? i + 1 : ""}</div>;
            })}
          </div>
          {/* Code content */}
          <div className="flex-1">
            {renderedLines}
          </div>
        </div>
      </div>

      {/* Footer */}
      <div className="border border-border border-t-0 px-4 py-2.5 flex items-center justify-between">
        <span className="font-mono text-[10px] text-muted-foreground/40">
          A2A-compatible · REST · JSON
        </span>
        <span className="font-mono text-[10px] text-muted-foreground/30">
          Coming soon — join the waitlist for early access
        </span>
      </div>
    </div>
  );
};

export default ApiTypingDemo;
