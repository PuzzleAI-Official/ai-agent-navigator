import { useState, useRef, useEffect } from "react";
import { Link, useLocation } from "react-router-dom";
import { motion, AnimatePresence } from "framer-motion";

type Stage = "describe" | "upload" | "testing" | "results";

interface Attachment {
  name: string;
  size: number;
  type: string;
}

interface Message {
  id: number;
  role: "user" | "assistant";
  content: string;
  attachments?: Attachment[];
}

interface Candidate {
  name: string;
  type: string;
  match: number;
  status: "pending" | "testing" | "done";
  performance?: number;
  speed?: number;
  cost?: number;
}

const MOCK_CANDIDATES: Candidate[] = [
  { name: "GPT-4o", type: "LLM", match: 94, status: "pending" },
  { name: "Claude 3.5 Sonnet", type: "LLM", match: 91, status: "pending" },
  { name: "Gemini 2.0 Flash", type: "LLM", match: 87, status: "pending" },
  { name: "Llama 3.1 70B", type: "LLM", match: 82, status: "pending" },
  { name: "Mistral Large", type: "LLM", match: 78, status: "pending" },
];

const Playground = () => {
  const location = useLocation();
  const initialMessage = (location.state as any)?.initialMessage || "";
  const [stage, setStage] = useState<Stage>("describe");
  const [messages, setMessages] = useState<Message[]>([
    {
      id: 0,
      role: "assistant",
      content:
        "Welcome to PuzzleAI. Describe the workflow or task you need an AI solution for, and I'll find the best candidates for you.",
    },
  ]);
  const [input, setInput] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [testProgress, setTestProgress] = useState(0);
  const chatEndRef = useRef<HTMLDivElement>(null);
  const [hasAutoSent, setHasAutoSent] = useState(false);

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // Simulate test progress
  useEffect(() => {
    if (stage !== "testing") return;
    const interval = setInterval(() => {
      setTestProgress((prev) => {
        if (prev >= 100) {
          clearInterval(interval);
          // Complete testing
          setCandidates((c) =>
            c.map((cand) => ({
              ...cand,
              status: "done" as const,
              performance: Math.floor(Math.random() * 20) + 78,
              speed: Math.floor(Math.random() * 400) + 100,
              cost: +(Math.random() * 0.08 + 0.01).toFixed(4),
            }))
          );
          setStage("results");
          setMessages((m) => [
            ...m,
            {
              id: Date.now(),
              role: "assistant",
              content:
                "Testing complete. All candidates have been evaluated against your workflow. Check the results panel for the full breakdown.",
            },
          ]);
          return 100;
        }
        // Update candidate statuses as progress advances
        const progressStep = prev + 2;
        setCandidates((c) =>
          c.map((cand, i) => {
            const threshold = (i + 1) * 18;
            if (progressStep >= threshold + 20) {
              return {
                ...cand,
                status: "done" as const,
                performance: Math.floor(Math.random() * 20) + 78,
                speed: Math.floor(Math.random() * 400) + 100,
                cost: +(Math.random() * 0.08 + 0.01).toFixed(4),
              };
            }
            if (progressStep >= threshold) {
              return { ...cand, status: "testing" as const };
            }
            return cand;
          })
        );
        return progressStep;
      });
    }, 200);
    return () => clearInterval(interval);
  }, [stage]);

  // Auto-send initial message from homepage
  useEffect(() => {
    if (initialMessage && !hasAutoSent && stage === "describe") {
      setHasAutoSent(true);
      setInput(initialMessage);
      // Trigger send after a brief delay
      setTimeout(() => {
        const userMsg: Message = { id: Date.now(), role: "user", content: initialMessage };
        setMessages((m) => [...m, userMsg]);
        setInput("");
        setTimeout(() => {
          setCandidates(MOCK_CANDIDATES);
          setMessages((m) => [
            ...m,
            {
              id: Date.now(),
              role: "assistant",
              content: `Great. I've identified ${MOCK_CANDIDATES.length} candidate AI solutions that match your workflow. You can review them in the right panel and add any specific tools manually.\n\nWhen ready, upload a test sample or describe your test criteria so we can benchmark them.`,
            },
          ]);
          setStage("upload");
        }, 1200);
      }, 500);
    }
  }, [initialMessage, hasAutoSent, stage]);

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files) return;
    const newAttachments: Attachment[] = Array.from(files).map((f) => ({
      name: f.name,
      size: f.size,
      type: f.type,
    }));
    setAttachments((prev) => [...prev, ...newAttachments]);
    e.target.value = "";
  };

  const removeAttachment = (index: number) => {
    setAttachments((prev) => prev.filter((_, i) => i !== index));
  };

  const formatFileSize = (bytes: number) => {
    if (bytes < 1024) return `${bytes}B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
  };

  const handleSend = () => {
    if (!input.trim() && attachments.length === 0) return;
    const userMsg: Message = { id: Date.now(), role: "user", content: input || (attachments.length > 0 ? `Uploaded ${attachments.length} file(s)` : ""), attachments: attachments.length > 0 ? [...attachments] : undefined };
    setMessages((m) => [...m, userMsg]);
    setInput("");
    setAttachments([]);

    if (stage === "describe") {
      setTimeout(() => {
        setCandidates(MOCK_CANDIDATES);
        setMessages((m) => [
          ...m,
          {
            id: Date.now(),
            role: "assistant",
            content: `Great. I've identified ${MOCK_CANDIDATES.length} candidate AI solutions that match your workflow. You can review them in the right panel and add any specific tools manually.\n\nWhen ready, upload a test sample or describe your test criteria so we can benchmark them.`,
          },
        ]);
        setStage("upload");
      }, 1200);
    } else if (stage === "upload") {
      setTimeout(() => {
        setMessages((m) => [
          ...m,
          {
            id: Date.now(),
            role: "assistant",
            content:
              "Understood. Starting benchmark tests now. Each candidate will be evaluated on performance, speed, and cost against your criteria.",
          },
        ]);
        setCandidates((c) => c.map((cand) => ({ ...cand, status: "pending" as const })));
        setTestProgress(0);
        setStage("testing");
      }, 800);
    } else if (stage === "results") {
      setTimeout(() => {
        setMessages((m) => [
          ...m,
          {
            id: Date.now(),
            role: "assistant",
            content:
              "You can re-run tests with different parameters, or select a candidate to proceed with integration. Let me know how you'd like to move forward.",
          },
        ]);
      }, 600);
    }
  };

  const stageLabels: Record<Stage, string> = {
    describe: "Describe Workflow",
    upload: "Define Test Criteria",
    testing: "Running Tests",
    results: "Results Ready",
  };

  return (
    <div className="h-screen flex flex-col bg-[#0f1117] text-[#e0e0e6] font-sans overflow-hidden">
      {/* Top Toolbar */}
      <header className="h-12 border-b border-[#1e2028] bg-[#13141b] flex items-center justify-between px-4 shrink-0">
        <div className="flex items-center gap-6">
          <Link to="/" className="flex items-center gap-0 mr-4">
            <span className="font-grotesk font-bold text-[15px] tracking-[-0.03em] text-[#e0e0e6]">
              puzzle
            </span>
            <span className="font-grotesk font-bold text-[15px] tracking-[-0.03em] text-[hsl(215,20%,50%)]">
              ai
            </span>
            <span className="font-grotesk font-bold text-[15px] text-[hsl(215,20%,50%)]">
              .
            </span>
          </Link>

          {/* Stage indicators */}
          <div className="hidden md:flex items-center gap-1">
            {(Object.keys(stageLabels) as Stage[]).map((s, i) => {
              const isActive = s === stage;
              const isPast =
                Object.keys(stageLabels).indexOf(s) <
                Object.keys(stageLabels).indexOf(stage);
              return (
                <div key={s} className="flex items-center gap-1">
                  {i > 0 && (
                    <div
                      className={`w-6 h-[1px] ${
                        isPast ? "bg-[hsl(215,20%,50%)]" : "bg-[#2a2b35]"
                      }`}
                    />
                  )}
                  <div
                    className={`px-3 py-1 text-[11px] font-grotesk font-medium uppercase tracking-[0.08em] transition-all ${
                      isActive
                        ? "bg-[hsl(215,20%,50%)]/15 text-[hsl(215,20%,60%)] border border-[hsl(215,20%,50%)]/30"
                        : isPast
                        ? "text-[#808090]"
                        : "text-[#40404d]"
                    }`}
                  >
                    {stageLabels[s]}
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        <div className="flex items-center gap-3">
          <button className="text-[11px] font-grotesk font-medium uppercase tracking-[0.06em] text-[#606070] hover:text-[#a0a0b0] transition-colors px-3 py-1.5">
            History
          </button>
          <button className="text-[11px] font-grotesk font-medium uppercase tracking-[0.06em] text-[#606070] hover:text-[#a0a0b0] transition-colors px-3 py-1.5">
            Settings
          </button>
        </div>
      </header>

      {/* Main content */}
      <div className="flex-1 flex overflow-hidden">
        {/* Left: Chat Panel */}
        <div className="w-[380px] lg:w-[420px] border-r border-[#1e2028] bg-[#111218] flex flex-col shrink-0">
          {/* Chat header */}
          <div className="px-5 py-3 border-b border-[#1e2028] flex items-center justify-between">
            <span className="font-grotesk font-semibold text-[12px] uppercase tracking-[0.1em] text-[#808090]">
              AI Assistant
            </span>
            <div className="w-2 h-2 bg-[hsl(215,20%,50%)] animate-pulse" style={{ transform: "rotate(45deg)" }} />
          </div>

          {/* Messages */}
          <div className="flex-1 overflow-y-auto px-5 py-4 space-y-4 scrollbar-thin">
            {messages.map((msg) => (
              <motion.div
                key={msg.id}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.3 }}
                className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}
              >
                <div
                  className={`max-w-[85%] px-4 py-3 text-[13px] leading-[1.7] ${
                    msg.role === "user"
                      ? "bg-[hsl(215,20%,50%)]/15 text-[#d0d0da] border border-[hsl(215,20%,50%)]/20"
                      : "bg-[#1a1b24] text-[#b0b0bc] border border-[#22232e]"
                  }`}
                >
                  {msg.content}
                </div>
              </motion.div>
            ))}
            <div ref={chatEndRef} />
          </div>

          {/* Input */}
          <div className="p-4 border-t border-[#1e2028]">
            <div className="flex items-center gap-2 bg-[#1a1b24] border border-[#2a2b35] focus-within:border-[hsl(215,20%,50%)]/40 transition-colors">
              <input
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleSend()}
                placeholder={
                  stage === "describe"
                    ? "Describe your workflow..."
                    : stage === "upload"
                    ? "Describe test criteria or paste a sample..."
                    : "Ask a follow-up question..."
                }
                className="flex-1 bg-transparent px-4 py-3 text-[13px] text-[#e0e0e6] placeholder:text-[#40404d] outline-none font-sans"
              />
              <button
                onClick={handleSend}
                className="px-4 py-3 text-[hsl(215,20%,50%)] hover:text-[hsl(215,20%,60%)] transition-colors"
              >
                <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                  <path
                    d="M14 2L7 9M14 2L10 14L7 9M14 2L2 6L7 9"
                    stroke="currentColor"
                    strokeWidth="1.2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </button>
            </div>
            {stage === "upload" && (
              <button className="mt-2 w-full py-2 border border-dashed border-[#2a2b35] text-[11px] font-grotesk uppercase tracking-[0.06em] text-[#606070] hover:text-[#a0a0b0] hover:border-[#40404d] transition-all">
                + Upload test sample
              </button>
            )}
          </div>
        </div>

        {/* Right: Results Panel */}
        <div className="flex-1 bg-[#0f1017] overflow-y-auto">
          <AnimatePresence mode="wait">
            {/* Describe stage — waiting */}
            {stage === "describe" && (
              <motion.div
                key="describe"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                className="h-full flex flex-col items-center justify-center px-8"
              >
                <div className="w-16 h-16 border border-[#2a2b35] flex items-center justify-center mb-6">
                  <div className="w-6 h-6 border border-[#40404d]" style={{ transform: "rotate(45deg)" }} />
                </div>
                <h2 className="font-grotesk font-semibold text-[15px] text-[#808090] mb-2">
                  Waiting for workflow description
                </h2>
                <p className="text-[13px] text-[#50505d] max-w-md text-center leading-relaxed">
                  Tell us about your use case in the chat panel. We'll analyze it and find AI solutions that match your requirements.
                </p>
              </motion.div>
            )}

            {/* Upload stage — show candidates */}
            {(stage === "upload" || stage === "testing" || stage === "results") && (
              <motion.div
                key="candidates"
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                className="p-6 lg:p-8"
              >
                {/* Section header */}
                <div className="flex items-center justify-between mb-6">
                  <div>
                    <h2 className="font-grotesk font-semibold text-[14px] text-[#c0c0cc] tracking-[-0.01em]">
                      Candidate AI Solutions
                    </h2>
                    <p className="text-[12px] text-[#50505d] mt-1">
                      {candidates.length} matches found
                    </p>
                  </div>
                  {stage === "upload" && (
                    <button className="text-[11px] font-grotesk font-medium uppercase tracking-[0.06em] text-[hsl(215,20%,50%)] hover:text-[hsl(215,20%,60%)] transition-colors px-3 py-1.5 border border-[hsl(215,20%,50%)]/20 hover:border-[hsl(215,20%,50%)]/40">
                      + Add manually
                    </button>
                  )}
                </div>

                {/* Test progress bar */}
                {stage === "testing" && (
                  <div className="mb-6">
                    <div className="flex items-center justify-between mb-2">
                      <span className="text-[11px] font-grotesk uppercase tracking-[0.08em] text-[#606070]">
                        Testing Progress
                      </span>
                      <span className="text-[11px] font-mono text-[hsl(215,20%,50%)]">
                        {testProgress}%
                      </span>
                    </div>
                    <div className="h-[2px] bg-[#1e2028] w-full">
                      <motion.div
                        className="h-full bg-[hsl(215,20%,50%)]"
                        style={{ width: `${testProgress}%` }}
                        transition={{ duration: 0.2 }}
                      />
                    </div>
                  </div>
                )}

                {/* Candidate cards */}
                <div className="space-y-3">
                  {candidates.map((cand, i) => (
                    <motion.div
                      key={cand.name}
                      initial={{ opacity: 0, x: 20 }}
                      animate={{ opacity: 1, x: 0 }}
                      transition={{ delay: i * 0.08 }}
                      className={`border p-4 lg:p-5 transition-all ${
                        cand.status === "testing"
                          ? "border-[hsl(215,20%,50%)]/30 bg-[hsl(215,20%,50%)]/[0.03]"
                          : cand.status === "done"
                          ? "border-[#22232e] bg-[#13141b]"
                          : "border-[#1e2028] bg-[#111218]"
                      }`}
                    >
                      <div className="flex items-start justify-between">
                        <div className="flex items-center gap-3">
                          {/* Status indicator */}
                          <div
                            className={`w-2.5 h-2.5 shrink-0 ${
                              cand.status === "testing"
                                ? "bg-[hsl(215,20%,50%)] animate-pulse"
                                : cand.status === "done"
                                ? "bg-[hsl(215,25%,60%)]"
                                : "bg-[#2a2b35]"
                            }`}
                            style={{ transform: "rotate(45deg)" }}
                          />
                          <div>
                            <h3 className="font-grotesk font-semibold text-[13px] text-[#d0d0da]">
                              {cand.name}
                            </h3>
                            <span className="text-[11px] text-[#50505d] font-mono">
                              {cand.type} · {cand.match}% match
                            </span>
                          </div>
                        </div>

                        {cand.status === "testing" && (
                          <span className="text-[10px] font-grotesk uppercase tracking-[0.1em] text-[hsl(215,20%,50%)] animate-pulse">
                            Testing...
                          </span>
                        )}
                      </div>

                      {/* Results */}
                      {cand.status === "done" && cand.performance != null && (
                        <motion.div
                          initial={{ opacity: 0, height: 0 }}
                          animate={{ opacity: 1, height: "auto" }}
                          transition={{ duration: 0.4 }}
                          className="mt-4 pt-4 border-t border-[#1e2028]"
                        >
                          <div className="grid grid-cols-3 gap-4">
                            <div>
                              <span className="text-[10px] font-grotesk uppercase tracking-[0.1em] text-[#50505d] block mb-1">
                                Performance
                              </span>
                              <span className="font-mono text-[18px] font-medium text-[#d0d0da]">
                                {cand.performance}
                              </span>
                              <span className="text-[11px] text-[#40404d] ml-0.5">/100</span>
                            </div>
                            <div>
                              <span className="text-[10px] font-grotesk uppercase tracking-[0.1em] text-[#50505d] block mb-1">
                                Speed
                              </span>
                              <span className="font-mono text-[18px] font-medium text-[#d0d0da]">
                                {cand.speed}
                              </span>
                              <span className="text-[11px] text-[#40404d] ml-0.5">ms</span>
                            </div>
                            <div>
                              <span className="text-[10px] font-grotesk uppercase tracking-[0.1em] text-[#50505d] block mb-1">
                                Cost
                              </span>
                              <span className="font-mono text-[18px] font-medium text-[#d0d0da]">
                                ${cand.cost}
                              </span>
                              <span className="text-[11px] text-[#40404d] ml-0.5">/call</span>
                            </div>
                          </div>

                          {/* Performance bar */}
                          <div className="mt-3 h-[3px] bg-[#1e2028] w-full">
                            <motion.div
                              className="h-full bg-gradient-to-r from-[hsl(215,20%,40%)] to-[hsl(215,25%,60%)]"
                              initial={{ width: 0 }}
                              animate={{ width: `${cand.performance}%` }}
                              transition={{ duration: 0.8, delay: 0.2 }}
                            />
                          </div>
                        </motion.div>
                      )}
                    </motion.div>
                  ))}
                </div>

                {/* Results summary */}
                {stage === "results" && (
                  <motion.div
                    initial={{ opacity: 0, y: 20 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ delay: 0.3 }}
                    className="mt-8 border border-[hsl(215,20%,50%)]/20 bg-[hsl(215,20%,50%)]/[0.03] p-6"
                  >
                    <h3 className="font-grotesk font-semibold text-[13px] uppercase tracking-[0.06em] text-[hsl(215,20%,60%)] mb-3">
                      Recommendation
                    </h3>
                    <p className="text-[13px] text-[#a0a0b0] leading-relaxed">
                      Based on your workflow requirements,{" "}
                      <span className="text-[#d0d0da] font-medium">
                        {candidates.sort((a, b) => (b.performance ?? 0) - (a.performance ?? 0))[0]?.name}
                      </span>{" "}
                      delivers the best balance of performance and cost. Consider{" "}
                      <span className="text-[#d0d0da] font-medium">
                        {candidates.sort((a, b) => (a.speed ?? 999) - (b.speed ?? 999))[0]?.name}
                      </span>{" "}
                      if latency is your primary concern.
                    </p>
                  </motion.div>
                )}
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </div>
    </div>
  );
};

export default Playground;
