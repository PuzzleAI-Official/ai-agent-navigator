import { motion } from "framer-motion";
import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import PuzzleBackground from "./PuzzleBackground";

const PLACEHOLDER_EXAMPLES = [
  "I need an AI to summarize legal documents",
  "I need an AI to generate marketing copy",
  "I need an AI to review my code",
];

const useTypingPlaceholder = (examples: string[], typingSpeed = 60, deletingSpeed = 35, pauseDuration = 2000) => {
  const [placeholder, setPlaceholder] = useState("");
  const [exampleIndex, setExampleIndex] = useState(0);
  const [isTyping, setIsTyping] = useState(true);
  const [charIndex, setCharIndex] = useState(0);
  const [isPaused, setIsPaused] = useState(false);

  useEffect(() => {
    const current = examples[exampleIndex];
    if (isPaused) {
      const timeout = setTimeout(() => {
        setIsPaused(false);
        setIsTyping(false);
      }, pauseDuration);
      return () => clearTimeout(timeout);
    }
    if (isTyping) {
      if (charIndex < current.length) {
        const timeout = setTimeout(() => {
          setPlaceholder(current.slice(0, charIndex + 1));
          setCharIndex(charIndex + 1);
        }, typingSpeed);
        return () => clearTimeout(timeout);
      } else {
        setIsPaused(true);
      }
    } else {
      if (charIndex > 0) {
        const timeout = setTimeout(() => {
          setPlaceholder(current.slice(0, charIndex - 1));
          setCharIndex(charIndex - 1);
        }, deletingSpeed);
        return () => clearTimeout(timeout);
      } else {
        setExampleIndex((exampleIndex + 1) % examples.length);
        setIsTyping(true);
      }
    }
  }, [charIndex, isTyping, isPaused, exampleIndex, examples, typingSpeed, deletingSpeed, pauseDuration]);

  return placeholder;
};

const Hero = () => {
  const [chatInput, setChatInput] = useState("");
  const navigate = useNavigate();
  const animatedPlaceholder = useTypingPlaceholder(PLACEHOLDER_EXAMPLES);

  const handleChatSubmit = () => {
    if (!chatInput.trim()) return;
    navigate("/playground", { state: { initialMessage: chatInput } });
  };

  return (
    <div className="relative" style={{ height: "280vh" }}>
      {/* Shared background for both sections */}
      <div className="absolute inset-0">
        <div className="sticky top-0 h-screen" style={{
          background: "linear-gradient(170deg, hsl(36 50% 91%) 0%, hsl(38 40% 94%) 30%, hsl(40 33% 97%) 55%, hsl(38 30% 95%) 100%)"
        }} />
      </div>
      <div className="absolute top-0 left-[10%] w-[80%] h-[75vh] bg-[radial-gradient(ellipse_at_50%_40%,hsl(33_55%_85%/0.55),transparent_65%)] pointer-events-none" />
      <div className="absolute top-[5vh] left-[20%] w-[60%] h-[55vh] bg-[radial-gradient(ellipse_at_50%_35%,hsl(260_20%_90%/0.18),transparent_55%)] pointer-events-none" />
      <div
        className="absolute inset-0 opacity-[0.015] pointer-events-none"
        style={{
          backgroundImage: `repeating-linear-gradient(-45deg, transparent, transparent 120px, hsl(var(--foreground)) 120px, hsl(var(--foreground)) 121px)`,
        }}
      />

      {/* Puzzle pieces layer */}
      <PuzzleBackground />

      {/* SECTION 1: Chat prompt — stays at top */}
      <div className="relative z-20 h-screen flex flex-col items-center justify-center">
        <div className="w-full max-w-[720px] px-8 flex flex-col items-center">
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 1, delay: 0.3, ease: [0.16, 1, 0.3, 1] }}
            className="text-center mb-10"
          >
            <h1 className="font-display leading-[1] tracking-[-0.03em] text-foreground mb-4 font-serif font-normal text-5xl">
              FIND YOUR LAST PUZZLE
            </h1>
          </motion.div>

          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.8, delay: 0.7 }}
            className="w-full"
          >
            <div className="relative bg-[hsl(220_20%_12%)] backdrop-blur-md border border-[hsl(220_15%_20%)] shadow-[0_8px_40px_-12px_hsl(220_30%_8%/0.5)] transition-all duration-300 focus-within:shadow-[0_12px_50px_-10px_hsl(220_30%_8%/0.6)] focus-within:border-[hsl(220_15%_28%)]">
              <textarea
                value={chatInput}
                onChange={(e) => setChatInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    handleChatSubmit();
                  }
                }}
                placeholder={animatedPlaceholder + "│"}
                rows={3}
                className="w-full bg-transparent px-6 py-5 text-[14px] text-[hsl(220_15%_90%)] placeholder:text-[hsl(220_10%_50%)] outline-none resize-none font-sans leading-relaxed rounded-none"
              />
              <div className="flex items-center justify-between px-5 pb-4">
                <span className="text-[11px] font-grotesk text-[hsl(220_10%_40%)] tracking-wide">
                  Press Enter to start
                </span>
                <button
                  onClick={handleChatSubmit}
                  className="group flex items-center gap-2 bg-[hsl(220_15%_90%)] text-[hsl(220_20%_12%)] px-5 py-2 font-grotesk font-semibold text-[11px] uppercase tracking-[0.08em] hover:bg-white hover:shadow-[0_8px_24px_-8px_hsl(220_20%_50%/0.3)] transition-all duration-300"
                >
                  <span>Start</span>
                  <svg width="12" height="12" viewBox="0 0 16 16" fill="none" className="transition-transform duration-300 group-hover:translate-x-1">
                    <path d="M3 8H13M13 8L9 4M13 8L9 12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
                  </svg>
                </button>
              </div>
            </div>
          </motion.div>
        </div>
      </div>

      {/* SECTION 2: Headline — appears as user scrolls past puzzle assembly */}
      <div className="relative z-20 min-h-screen flex flex-col justify-end pb-12 md:pb-16" style={{ marginTop: "80vh" }}>
        <div className="max-w-[1400px] mx-auto w-full px-8">
          <motion.div
            initial={{ opacity: 0 }}
            whileInView={{ opacity: 1 }}
            viewport={{ once: true, margin: "-100px" }}
            transition={{ duration: 0.8 }}
            className="flex items-center gap-4 mb-8"
          >
            <div className="w-8 h-[2px] bg-accent/30" style={{ transform: "skewX(-20deg)" }} />
            <span className="font-grotesk font-semibold text-[11px] uppercase tracking-[0.25em] text-muted-foreground">
              The AI hiring platform
            </span>
          </motion.div>

          <motion.h2
            initial={{ opacity: 0, y: 40 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true, margin: "-80px" }}
            transition={{ duration: 1.2, ease: [0.16, 1, 0.3, 1] }}
            className="font-display text-[clamp(2.8rem,6.5vw,6.5rem)] leading-[0.9] tracking-[-0.03em] text-foreground"
          >
            We help you find the <span className="italic text-gradient">right</span> AI.
          </motion.h2>

          <motion.p
            initial={{ opacity: 0, y: 20 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true, margin: "-50px" }}
            transition={{ duration: 0.7, delay: 0.2 }}
            className="mt-6 text-[15px] md:text-[16px] text-muted-foreground max-w-[560px] leading-[1.75]"
          >
            Describe your workflow. We test every AI solution against your real use cases and deliver three verdicts: performance, speed, cost.
          </motion.p>

          <motion.div
            initial={{ opacity: 0, y: 20 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true }}
            transition={{ duration: 0.6, delay: 0.4 }}
            className="mt-10"
          >
            <a
              href="#how-it-works"
              className="group font-grotesk font-medium text-[12px] uppercase tracking-[0.1em] text-muted-foreground hover:text-foreground transition-colors duration-300 flex items-center gap-2"
            >
              <span className="border-b border-muted-foreground/30 pb-0.5 group-hover:border-accent transition-colors duration-300">Learn more</span>
            </a>
          </motion.div>
        </div>
      </div>
    </div>
  );
};

export default Hero;
