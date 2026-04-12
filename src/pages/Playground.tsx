import { useState, useRef, useEffect } from "react";
import { Link, useLocation } from "react-router-dom";
import { motion, AnimatePresence } from "framer-motion";
import ReactMarkdown from "react-markdown";
import { usePipelineRun } from "@/hooks/usePipelineRun";
import { PipelineVisualization } from "@/components/playground/PipelineVisualization";
import { ActivityFeed } from "@/components/playground/ActivityFeed";
import { CandidateCard } from "@/components/playground/CandidateCard";
import { ResultsComparison } from "@/components/playground/ResultsComparison";
import { SearchingVisualization } from "@/components/playground/SearchingVisualization";
import type { Attachment } from "@/types/pipeline";

const Playground = () => {
  const location = useLocation();
  const initialMessage = (location.state as any)?.initialMessage || "";

  const {
    stage,
    messages,
    candidates,
    pipelineProgress,
    costAccumulator,
    isLoading,
    handleSend,
    handleCancel,
    activityEntries,
    pipelineNodes,
  } = usePipelineRun();

  const [input, setInput] = useState("");
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [activeTab, setActiveTab] = useState<"chat" | "activity">("chat");
  const fileInputRef = useRef<HTMLInputElement>(null);
  const chatEndRef = useRef<HTMLDivElement>(null);
  const [hasAutoSent, setHasAutoSent] = useState(false);

  // Auto-scroll chat
  useEffect(() => {
    if (activeTab === "chat") {
      chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages, activeTab]);

  // Auto-switch to activity tab when pipeline starts
  useEffect(() => {
    if (stage === "pipeline" && activityEntries.length > 0) {
      setActiveTab("activity");
    }
  }, [stage, activityEntries.length]);

  // Auto-send initial message from homepage
  useEffect(() => {
    if (initialMessage && !hasAutoSent && stage === "conversation") {
      setHasAutoSent(true);
      setTimeout(() => handleSend(initialMessage, []), 500);
    }
  }, [initialMessage, hasAutoSent, stage, handleSend]);

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files;
    if (!files) return;
    const newFiles = Array.from(files);
    setPendingFiles((prev) => [...prev, ...newFiles]);
    setAttachments((prev) => [
      ...prev,
      ...newFiles.map((f) => ({ name: f.name, size: f.size, type: f.type })),
    ]);
    e.target.value = "";
  };

  const removeAttachment = (index: number) => {
    setPendingFiles((prev) => prev.filter((_, i) => i !== index));
    setAttachments((prev) => prev.filter((_, i) => i !== index));
  };

  const formatFileSize = (bytes: number) => {
    if (bytes < 1024) return `${bytes}B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
  };

  const onSend = () => {
    if (!input.trim() && pendingFiles.length === 0) return;
    handleSend(input, pendingFiles);
    setInput("");
    setPendingFiles([]);
    setAttachments([]);
  };

  // Stage indicator labels
  const stageKeys = ["conversation", "research", "screening", "testing", "results"];
  const stageLabels: Record<string, string> = {
    conversation: "Conversation",
    research: "Research",
    screening: "Screening",
    testing: "Testing",
    results: "Results",
  };

  const getDisplayStage = (): string => {
    if (stage === "conversation") return "conversation";
    if (stage === "results") return "results";
    const completed = pipelineProgress.agents_completed;
    if (completed.includes("agent_5")) return "results";
    if (completed.includes("agent_4")) return "testing";
    if (completed.includes("agent_2")) return "screening";
    return "research";
  };
  const displayStage = getDisplayStage();

  const showActivityTab = stage !== "conversation" || activityEntries.length > 0;

  return (
    <div className="h-screen flex flex-col bg-[#0a0a0b] text-white/95 font-sans overflow-hidden">
      {/* Header */}
      <header className="h-12 border-b border-white/[0.06] bg-white/[0.03] backdrop-blur-xl flex items-center justify-between px-4 shrink-0">
        <div className="flex items-center gap-6">
          <Link to="/" className="flex items-center gap-0 mr-4">
            <span className="font-grotesk font-bold text-[15px] tracking-[-0.03em] text-[#f5f5f7]">puzzle</span>
            <span className="font-grotesk font-bold text-[15px] tracking-[-0.03em] text-blue-400/70">ai</span>
            <span className="font-grotesk font-bold text-[15px] text-blue-400/70">.</span>
          </Link>
          <div className="hidden md:flex items-center gap-1">
            {stageKeys.map((s, i) => {
              const isActive = s === displayStage;
              const isPast = stageKeys.indexOf(s) < stageKeys.indexOf(displayStage);
              return (
                <div key={s} className="flex items-center gap-1">
                  {i > 0 && (
                    <div className={`w-6 h-[1px] ${isPast ? "bg-blue-400/40" : "bg-white/[0.06]"}`} />
                  )}
                  <div
                    className={`px-3 py-1 rounded-md text-[11px] font-grotesk font-medium uppercase tracking-[0.08em] transition-all ${
                      isActive
                        ? "bg-blue-500/10 text-blue-300/90 border border-blue-400/20"
                        : isPast
                        ? "text-white/40"
                        : "text-white/15"
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
          {stage === "pipeline" && (
            <button
              onClick={handleCancel}
              className="text-[11px] font-grotesk font-medium uppercase tracking-[0.06em] text-red-400/90 hover:text-red-300 transition-colors px-3 py-1.5 rounded-md border border-red-400/15 hover:border-red-400/30 hover:bg-red-400/5"
            >
              Cancel Run
            </button>
          )}
          {costAccumulator > 0 && (
            <div className="flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-white/[0.04] border border-white/[0.06]">
              <svg width="12" height="12" viewBox="0 0 16 16" fill="none" className="text-white/25">
                <circle cx="8" cy="8" r="6.5" stroke="currentColor" strokeWidth="1.2" />
                <path d="M8 4.5v7M5.5 6.5h5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
              </svg>
              <span className="text-[11px] font-mono text-white/50">{(costAccumulator * 20).toFixed(2)}</span>
              <span className="text-[9px] font-grotesk text-white/25 uppercase tracking-[0.04em]">credits used</span>
            </div>
          )}
          <button className="text-[11px] font-grotesk font-medium uppercase tracking-[0.06em] text-white/25 hover:text-white/50 transition-colors px-3 py-1.5">
            History
          </button>
          <button className="text-[11px] font-grotesk font-medium uppercase tracking-[0.06em] text-white/25 hover:text-white/50 transition-colors px-3 py-1.5">
            Settings
          </button>
        </div>
      </header>

      {/* Main content */}
      <div className="flex-1 flex overflow-hidden">
        {/* Left Panel */}
        <div className="w-[380px] lg:w-[420px] border-r border-white/[0.06] bg-white/[0.02] flex flex-col shrink-0">
          {/* Tab header */}
          <div className="px-5 py-2 border-b border-white/[0.06] flex items-center justify-between">
            <div className="flex items-center gap-3">
              <button
                onClick={() => setActiveTab("chat")}
                className={`text-[11px] font-grotesk font-semibold uppercase tracking-[0.1em] px-2 py-1 transition-all ${
                  activeTab === "chat"
                    ? "text-white/90 border-b border-blue-400/50"
                    : "text-white/25 hover:text-white/50"
                }`}
              >
                Chat
              </button>
              {showActivityTab && (
                <motion.button
                  initial={{ opacity: 0, x: -10 }}
                  animate={{ opacity: 1, x: 0 }}
                  onClick={() => setActiveTab("activity")}
                  className={`text-[11px] font-grotesk font-semibold uppercase tracking-[0.1em] px-2 py-1 transition-all ${
                    activeTab === "activity"
                      ? "text-white/90 border-b border-blue-400/50"
                      : "text-white/25 hover:text-white/50"
                  }`}
                >
                  Activity
                  {stage === "pipeline" && (
                    <span className="ml-1.5 w-1.5 h-1.5 bg-[hsl(220,14%,56%)] rounded-full inline-block animate-pulse" />
                  )}
                </motion.button>
              )}
            </div>
            <div className="flex items-center gap-2">
              {isLoading && (
                <span className="text-[10px] font-grotesk uppercase tracking-[0.1em] text-blue-400/70 animate-pulse">
                  Thinking...
                </span>
              )}
              <div className="w-2 h-2 bg-blue-400/60 animate-pulse" style={{ transform: "rotate(45deg)" }} />
            </div>
          </div>

          {/* Tab content */}
          {activeTab === "chat" ? (
            <>
              {/* Chat Messages */}
              <div className="flex-1 overflow-y-auto px-5 py-4 space-y-5 scrollbar-thin">
                {messages.map((msg) => (
                  <motion.div
                    key={msg.id}
                    initial={{ opacity: 0, y: 8 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ duration: 0.3 }}
                    className={`flex gap-3 ${msg.role === "user" ? "flex-row-reverse" : "flex-row"}`}
                  >
                    {/* Avatar */}
                    <div className={`w-7 h-7 shrink-0 flex items-center justify-center rounded-full mt-0.5 ${
                      msg.role === "user"
                        ? "bg-blue-500/15 border border-blue-400/20"
                        : "bg-white/[0.04] border border-white/[0.08]"
                    }`}>
                      {msg.role === "user" ? (
                        <svg width="12" height="12" viewBox="0 0 16 16" fill="none">
                          <circle cx="8" cy="5" r="3" stroke="rgba(255,255,255,0.4)" strokeWidth="1.2" />
                          <path d="M2 14c0-3.3 2.7-6 6-6s6 2.7 6 6" stroke="rgba(255,255,255,0.4)" strokeWidth="1.2" />
                        </svg>
                      ) : (
                        <div className="w-2.5 h-2.5 bg-blue-400/60" style={{ transform: "rotate(45deg)" }} />
                      )}
                    </div>

                    {/* Message content */}
                    <div
                      className={`max-w-[80%] px-4 py-3 text-[14px] leading-[1.7] ${
                        msg.role === "user"
                          ? "bg-blue-500/10 text-white/90 border border-blue-400/15 rounded-2xl rounded-tr-sm"
                          : "bg-white/[0.04] text-white/80 border border-white/[0.06] rounded-2xl rounded-tl-sm"
                      }`}
                    >
                      {msg.role === "assistant" ? (
                        <div className="prose prose-invert prose-sm max-w-none [&_p]:my-1 [&_ul]:my-1 [&_ol]:my-1 [&_li]:my-0.5 [&_code]:bg-[#141416] [&_code]:px-1 [&_code]:py-0.5 [&_code]:text-[12px] [&_code]:text-white/55">
                          <ReactMarkdown>{msg.content}</ReactMarkdown>
                        </div>
                      ) : (
                        <span className="whitespace-pre-wrap">{msg.content}</span>
                      )}
                      {msg.attachments && msg.attachments.length > 0 && (
                        <div className="mt-2 space-y-1">
                          {msg.attachments.map((att, i) => (
                            <div key={i} className="flex items-center gap-2 px-2 py-1.5 bg-[#141416]/50 border border-[#2c2c2e]/50 text-[11px] rounded">
                              <svg width="12" height="12" viewBox="0 0 16 16" fill="none" className="shrink-0 text-white/35">
                                <path d="M9 1H4a1 1 0 00-1 1v12a1 1 0 001 1h8a1 1 0 001-1V5L9 1z" stroke="currentColor" strokeWidth="1.2" />
                                <path d="M9 1v4h4" stroke="currentColor" strokeWidth="1.2" />
                              </svg>
                              <span className="text-white/55 truncate">{att.name}</span>
                              <span className="text-[#3a3a3c] shrink-0">{formatFileSize(att.size)}</span>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  </motion.div>
                ))}

                {/* Typing indicator */}
                {isLoading && (
                  <motion.div
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    className="flex gap-3"
                  >
                    <div className="w-7 h-7 shrink-0 flex items-center justify-center rounded-full bg-white/[0.04] border border-white/[0.08]">
                      <div className="w-2.5 h-2.5 bg-blue-400/60" style={{ transform: "rotate(45deg)" }} />
                    </div>
                    <div className="bg-white/[0.04] border border-white/[0.06] rounded-2xl rounded-tl-sm px-4 py-3 flex items-center gap-1.5">
                      <motion.span animate={{ opacity: [0.2, 0.8, 0.2] }} transition={{ duration: 1.2, repeat: Infinity, delay: 0 }} className="w-1.5 h-1.5 bg-white/50 rounded-full" />
                      <motion.span animate={{ opacity: [0.2, 0.8, 0.2] }} transition={{ duration: 1.2, repeat: Infinity, delay: 0.2 }} className="w-1.5 h-1.5 bg-white/50 rounded-full" />
                      <motion.span animate={{ opacity: [0.2, 0.8, 0.2] }} transition={{ duration: 1.2, repeat: Infinity, delay: 0.4 }} className="w-1.5 h-1.5 bg-white/50 rounded-full" />
                    </div>
                  </motion.div>
                )}

                <div ref={chatEndRef} />
              </div>
            </>
          ) : (
            <ActivityFeed entries={activityEntries} />
          )}

          {/* Input (always visible) */}
          <div className="p-4 border-t border-white/[0.06]">
            {attachments.length > 0 && (
              <div className="mb-2 space-y-1">
                {attachments.map((att, i) => (
                  <div key={i} className="flex items-center justify-between gap-2 px-3 py-2 bg-white/[0.03] border border-white/[0.06] text-[11px]">
                    <div className="flex items-center gap-2 min-w-0">
                      <svg width="12" height="12" viewBox="0 0 16 16" fill="none" className="shrink-0 text-white/35">
                        <path d="M9 1H4a1 1 0 00-1 1v12a1 1 0 001 1h8a1 1 0 001-1V5L9 1z" stroke="currentColor" strokeWidth="1.2" />
                        <path d="M9 1v4h4" stroke="currentColor" strokeWidth="1.2" />
                      </svg>
                      <span className="text-white/55 truncate">{att.name}</span>
                      <span className="text-[#3a3a3c] shrink-0">{formatFileSize(att.size)}</span>
                    </div>
                    <button onClick={() => removeAttachment(i)} className="text-white/20 hover:text-white/55 transition-colors shrink-0">
                      <svg width="10" height="10" viewBox="0 0 10 10" fill="none">
                        <path d="M1 1l8 8M9 1l-8 8" stroke="currentColor" strokeWidth="1.2" />
                      </svg>
                    </button>
                  </div>
                ))}
              </div>
            )}
            <input ref={fileInputRef} type="file" multiple onChange={handleFileSelect} className="hidden" />
            <div className="flex items-center gap-0 rounded-xl bg-white/[0.04] border border-white/[0.08] focus-within:border-blue-400/30 transition-colors">
              <button
                onClick={() => fileInputRef.current?.click()}
                className="px-3 py-3 text-white/20 hover:text-white/55 transition-colors shrink-0"
                title="Attach files"
              >
                <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                  <path d="M14 8.5l-5.5 5.5a3.5 3.5 0 01-5-5L9 3.5a2 2 0 013 3L6.5 12a.5.5 0 01-1-1L11 5.5" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </button>
              <input
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && !isLoading && onSend()}
                placeholder={stage === "conversation" ? "Describe your workflow..." : "Ask a follow-up question..."}
                disabled={isLoading}
                className="flex-1 bg-transparent px-2 py-3 text-[13px] text-[#f5f5f7] placeholder:text-[#3a3a3c] outline-none font-sans disabled:opacity-50"
              />
              <button
                onClick={onSend}
                disabled={isLoading}
                className="px-4 py-3 text-blue-400/70 hover:text-blue-300/80 transition-colors disabled:opacity-50"
              >
                <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                  <path d="M14 2L7 9M14 2L10 14L7 9M14 2L2 6L7 9" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </button>
            </div>
          </div>
        </div>

        {/* Right Panel */}
        <div className="flex-1 bg-[#0a0a0b] flex flex-col overflow-hidden">
          {/* Pipeline Visualization */}
          <PipelineVisualization nodes={pipelineNodes} visible={stage !== "conversation"} />

          {/* Scrollable content */}
          <div className="flex-1 overflow-y-auto">
            <AnimatePresence mode="wait">
              {/* Waiting state */}
              {stage === "conversation" && candidates.length === 0 && (
                <motion.div
                  key="waiting"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  className="h-full flex flex-col items-center justify-center px-8"
                >
                  <div className="w-16 h-16 border border-[#2c2c2e] flex items-center justify-center mb-6">
                    <div className="w-6 h-6 border border-[#3a3a3c]" style={{ transform: "rotate(45deg)" }} />
                  </div>
                  <h2 className="font-grotesk font-semibold text-[15px] text-white/35 mb-2">
                    Waiting for workflow description
                  </h2>
                  <p className="text-[13px] text-white/20 max-w-md text-center leading-relaxed">
                    Tell us about your use case in the chat panel. We'll analyze it and find AI solutions that match your requirements.
                  </p>
                </motion.div>
              )}

              {/* Pipeline stage — searching visualization (before candidates appear) */}
              {stage === "pipeline" && candidates.length === 0 && (
                <motion.div
                  key="searching"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  exit={{ opacity: 0 }}
                  className="h-full"
                >
                  <SearchingVisualization
                    message={pipelineProgress.current_agent || "Discovering AI solutions..."}
                  />
                </motion.div>
              )}

              {/* Pipeline stage — show candidate cards */}
              {stage === "pipeline" && candidates.length > 0 && (
                <motion.div
                  key="candidates"
                  initial={{ opacity: 0, y: 20 }}
                  animate={{ opacity: 1, y: 0 }}
                  className="p-6 lg:p-8"
                >
                  <div className="flex items-center justify-between mb-5">
                    <div>
                      <h2 className="font-display text-[18px] text-white/90 tracking-[-0.01em]">
                        Candidates
                      </h2>
                      <p className="text-[12px] text-white/25 mt-0.5 font-sans">{candidates.length} discovered</p>
                    </div>
                    {pipelineProgress.current_agent && (
                      <span className="text-[11px] font-grotesk tracking-[0.04em] text-blue-400/60 animate-pulse">
                        {pipelineProgress.current_agent}
                      </span>
                    )}
                  </div>
                  <div className="space-y-2">
                    <AnimatePresence>
                      {candidates.map((cand, i) => (
                        <CandidateCard key={cand.name} candidate={cand} index={i} />
                      ))}
                    </AnimatePresence>
                  </div>
                </motion.div>
              )}

              {/* Results stage — side-by-side comparison */}
              {stage === "results" && candidates.length > 0 && (
                <motion.div
                  key="results"
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                >
                  <ResultsComparison candidates={candidates} />
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        </div>
      </div>
    </div>
  );
};

export default Playground;
