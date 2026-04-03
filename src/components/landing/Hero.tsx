import { motion } from "framer-motion";
import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import heroMetal from "@/assets/hero-metal.png";

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
  const containerRef = useRef<HTMLDivElement>(null);
  const [mousePos, setMousePos] = useState({ x: 0.5, y: 0.5 });
  const [chatInput, setChatInput] = useState("");
  const navigate = useNavigate();
  const animatedPlaceholder = useTypingPlaceholder(PLACEHOLDER_EXAMPLES);

  const handleMouseMove = useCallback((e: React.MouseEvent) => {
    if (!containerRef.current) return;
    const rect = containerRef.current.getBoundingClientRect();
    setMousePos({
      x: (e.clientX - rect.left) / rect.width,
      y: (e.clientY - rect.top) / rect.height,
    });
  }, []);

  const offsetX = (mousePos.x - 0.5) * 30;
  const offsetY = (mousePos.y - 0.5) * 22;

  const handleChatSubmit = () => {
    if (!chatInput.trim()) return;
    navigate("/playground", { state: { initialMessage: chatInput } });
  };

  return (
    <>
      {/* SECTION 1: Initial view — chat prompt */}
      <section
        ref={containerRef}
        onMouseMove={handleMouseMove}
        className="relative min-h-screen overflow-hidden flex flex-col items-center justify-center"
      >
        {/* Background */}
        <div className="absolute inset-0" style={{
          background: "linear-gradient(170deg, hsl(36 50% 91%) 0%, hsl(38 40% 94%) 30%, hsl(40 33% 97%) 55%, hsl(38 30% 95%) 100%)"
        }} />
        <div className="absolute top-[-5%] left-[10%] w-[80%] h-[75%] bg-[radial-gradient(ellipse_at_50%_40%,hsl(33_55%_85%/0.55),transparent_65%)] pointer-events-none" />
        <div className="absolute top-[5%] left-[20%] w-[60%] h-[55%] bg-[radial-gradient(ellipse_at_50%_35%,hsl(260_20%_90%/0.18),transparent_55%)] pointer-events-none" />
        <div className="absolute bottom-0 left-0 w-full h-[35%] bg-gradient-to-t from-background to-transparent pointer-events-none" />
        <div
          className="absolute inset-0 opacity-[0.015] pointer-events-none"
          style={{
            backgroundImage: `repeating-linear-gradient(-45deg, transparent, transparent 120px, hsl(var(--foreground)) 120px, hsl(var(--foreground)) 121px)`,
          }}
        />

        {/* Centered chat prompt */}
        <div className="relative z-20 w-full max-w-[720px] px-8 flex flex-col items-center">
          <motion.div
            initial={{ opacity: 0, y: 20 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 1, delay: 0.3, ease: [0.16, 1, 0.3, 1] }}
            className="text-center mb-10"
          >
            <h1 className="font-display text-[clamp(2.2rem,5vw,4.5rem)] leading-[1] tracking-[-0.03em] text-foreground mb-4 font-serif">
              Find Your Last Puzzle
            </h1>
            <p className="text-[14px] md:text-[15px] text-muted-foreground leading-relaxed max-w-md mx-auto">
              {"\n"}
            </p>
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
                className="w-full bg-transparent px-6 py-5 text-[14px] text-[hsl(220_15%_90%)] placeholder:text-[hsl(220_10%_50%)] outline-none resize-none font-sans leading-relaxed"
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
      </section>

      {/* SECTION 2: Scroll-reveal — headline + sculpture */}
      <section
        className="relative min-h-screen overflow-hidden"
      >
        {/* Background continuity */}
        <div className="absolute inset-0 bg-background" />
        <div
          className="absolute inset-0 opacity-[0.015] pointer-events-none"
          style={{
            backgroundImage: `repeating-linear-gradient(-45deg, transparent, transparent 120px, hsl(var(--foreground)) 120px, hsl(var(--foreground)) 121px)`,
          }}
        />

        {/* Sculpture */}
        <div className="absolute top-0 left-0 w-full h-[70vh] flex justify-center items-center pointer-events-none">
          <motion.div
            initial={{ opacity: 0, scale: 0.4 }}
            whileInView={{ opacity: 1, scale: 1 }}
            viewport={{ once: true, margin: "-100px" }}
            transition={{ duration: 2.5, ease: [0.16, 1, 0.3, 1] }}
            className="absolute w-[90vw] h-[60vw] max-w-[1100px] max-h-[700px]"
            style={{
              background: "radial-gradient(ellipse, hsl(33 55% 83% / 0.35) 0%, hsl(33 45% 88% / 0.15) 35%, hsl(260 20% 90% / 0.06) 55%, transparent 75%)",
            }}
          />
          <motion.img
            src={heroMetal}
            alt=""
            width={1920}
            height={1080}
            initial={{ opacity: 0, scale: 0.8, y: 60 }}
            whileInView={{ opacity: 1, scale: 1, y: 0 }}
            viewport={{ once: true, margin: "-50px" }}
            transition={{ duration: 2, ease: [0.16, 1, 0.3, 1] }}
            className="relative z-10 w-[90vw] md:w-[60vw] lg:w-[52vw] max-w-[850px] h-auto"
            style={{
              filter: "drop-shadow(0 50px 100px rgba(80, 55, 30, 0.18)) drop-shadow(0 20px 40px rgba(60, 45, 30, 0.12)) drop-shadow(0 5px 15px rgba(40, 30, 20, 0.06))",
            }}
          />
        </div>

        {/* Headline content */}
        <div className="relative z-20 min-h-screen flex flex-col justify-end pb-12 md:pb-16">
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
      </section>
    </>
  );
};

export default Hero;
