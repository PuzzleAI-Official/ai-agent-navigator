import { motion } from "framer-motion";
import { useEffect, useRef, useState, useCallback } from "react";
import heroCrystal from "@/assets/hero-crystal.png";

const Hero = () => {
  const containerRef = useRef<HTMLDivElement>(null);
  const [mousePos, setMousePos] = useState({ x: 0.5, y: 0.5 });

  const handleMouseMove = useCallback((e: React.MouseEvent) => {
    if (!containerRef.current) return;
    const rect = containerRef.current.getBoundingClientRect();
    setMousePos({
      x: (e.clientX - rect.left) / rect.width,
      y: (e.clientY - rect.top) / rect.height,
    });
  }, []);

  // Parallax values based on mouse
  const offsetX = (mousePos.x - 0.5) * 20;
  const offsetY = (mousePos.y - 0.5) * 15;
  const crystalRotate = (mousePos.x - 0.5) * 4;

  // Typing effect
  const fullText = "Describe your workflow. We test 200+ AI solutions against your real use cases — and deliver three verdicts.";
  const [displayed, setDisplayed] = useState("");
  const [started, setStarted] = useState(false);

  useEffect(() => {
    const timeout = setTimeout(() => setStarted(true), 2200);
    return () => clearTimeout(timeout);
  }, []);

  useEffect(() => {
    if (!started) return;
    let i = 0;
    const interval = setInterval(() => {
      setDisplayed(fullText.slice(0, i + 1));
      i++;
      if (i >= fullText.length) clearInterval(interval);
    }, 20);
    return () => clearInterval(interval);
  }, [started]);

  return (
    <section
      ref={containerRef}
      onMouseMove={handleMouseMove}
      className="relative min-h-screen overflow-hidden"
    >
      {/* ── Warm gradient background ── */}
      <div className="absolute inset-0 bg-gradient-to-b from-[hsl(38,40%,95%)] via-background to-background" />
      <div className="absolute top-0 right-0 w-[70%] h-[80%] bg-[radial-gradient(ellipse_at_70%_30%,hsl(35_50%_88%/0.5),transparent_65%)]" />
      <div className="absolute bottom-0 left-0 w-[50%] h-[50%] bg-[radial-gradient(ellipse_at_20%_80%,hsl(225_40%_90%/0.3),transparent_60%)]" />

      {/* Subtle light beams from crystal */}
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        transition={{ delay: 1.5, duration: 2 }}
        className="absolute top-[10%] left-[30%] w-[40%] h-[80%] pointer-events-none"
        style={{
          background: "conic-gradient(from 180deg at 50% 40%, transparent 0deg, hsl(225 45% 80% / 0.04) 30deg, transparent 60deg, hsl(35 60% 80% / 0.05) 120deg, transparent 180deg, hsl(280 30% 85% / 0.03) 240deg, transparent 300deg)",
          transform: `translate(${offsetX * 0.3}px, ${offsetY * 0.3}px)`,
        }}
      />

      {/* ── Crystal Centerpiece ── */}
      <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
        {/* Glow behind crystal */}
        <motion.div
          initial={{ opacity: 0, scale: 0.8 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ delay: 0.5, duration: 2, ease: [0.16, 1, 0.3, 1] }}
          className="absolute w-[500px] h-[500px] md:w-[650px] md:h-[650px]"
          style={{
            background: "radial-gradient(circle, hsl(225 45% 80% / 0.12) 0%, hsl(35 50% 85% / 0.08) 40%, transparent 70%)",
            transform: `translate(${offsetX * 0.5}px, ${offsetY * 0.5}px)`,
          }}
        />

        {/* Crystal image */}
        <motion.div
          initial={{ opacity: 0, scale: 0.6, y: 40 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          transition={{ duration: 1.8, delay: 0.3, ease: [0.16, 1, 0.3, 1] }}
          className="relative z-10"
          style={{
            transform: `translate(${offsetX}px, ${offsetY}px) rotate(${crystalRotate}deg)`,
            transition: "transform 0.3s ease-out",
          }}
        >
          <img
            src={heroCrystal}
            alt="AI Evaluation Crystal"
            width={1920}
            height={1080}
            className="w-[420px] md:w-[550px] lg:w-[650px] h-auto drop-shadow-[0_20px_60px_rgba(100,80,160,0.15)]"
          />

          {/* Prismatic light shimmer overlay */}
          <motion.div
            className="absolute inset-0"
            animate={{
              background: [
                "linear-gradient(135deg, transparent 30%, hsl(225 60% 85% / 0.1) 50%, transparent 70%)",
                "linear-gradient(135deg, transparent 40%, hsl(35 60% 85% / 0.1) 60%, transparent 80%)",
                "linear-gradient(135deg, transparent 30%, hsl(225 60% 85% / 0.1) 50%, transparent 70%)",
              ],
            }}
            transition={{ duration: 4, repeat: Infinity, ease: "easeInOut" }}
          />
        </motion.div>

        {/* Floating micro-particles around crystal */}
        {[...Array(8)].map((_, i) => {
          const angle = (i / 8) * Math.PI * 2;
          const radius = 200 + (i % 3) * 80;
          return (
            <motion.div
              key={i}
              className="absolute w-1 h-1 bg-accent/20 rounded-full"
              style={{
                left: `calc(50% + ${Math.cos(angle) * radius}px)`,
                top: `calc(50% + ${Math.sin(angle) * radius}px)`,
              }}
              animate={{
                y: [0, -15, 0],
                opacity: [0.2, 0.5, 0.2],
                scale: [1, 1.5, 1],
              }}
              transition={{
                duration: 3 + i * 0.5,
                repeat: Infinity,
                delay: i * 0.4,
                ease: "easeInOut",
              }}
            />
          );
        })}
      </div>

      {/* ── Typography & Content ── */}
      <div className="relative z-20 min-h-screen flex flex-col justify-between pb-12 md:pb-20">
        <div className="max-w-[1400px] mx-auto w-full px-8 flex-1 flex flex-col justify-center">
          <div className="grid md:grid-cols-2 gap-12 items-center pt-24 md:pt-0">
            {/* Left: Text content */}
            <div>
              <motion.div
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                transition={{ delay: 0.3, duration: 0.8 }}
                className="flex items-center gap-4 mb-10"
              >
                <motion.div
                  className="w-12 h-px bg-gradient-to-r from-accent to-accent/10"
                  initial={{ scaleX: 0 }}
                  animate={{ scaleX: 1 }}
                  transition={{ delay: 0.5, duration: 1 }}
                  style={{ transformOrigin: "left" }}
                />
                <span className="font-mono text-[11px] uppercase tracking-[0.3em] text-muted-foreground">
                  The AI hiring platform
                </span>
              </motion.div>

              <motion.h1
                initial={{ opacity: 0, y: 50 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 1.2, delay: 0.4, ease: [0.16, 1, 0.3, 1] }}
                className="font-display text-[clamp(3.5rem,8vw,7.5rem)] leading-[0.85] tracking-[-0.04em] text-foreground"
              >
                Find the
                <br />
                <span className="italic text-gradient">right</span> AI.
              </motion.h1>

              <motion.p
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.7, delay: 1 }}
                className="mt-8 text-base md:text-[16px] text-muted-foreground max-w-[400px] leading-[1.8]"
              >
                {displayed}
                {started && displayed.length < fullText.length && (
                  <span className="animate-pulse text-accent ml-0.5">|</span>
                )}
              </motion.p>

              <motion.div
                initial={{ opacity: 0, y: 20 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.6, delay: 1.3 }}
                className="mt-10 flex flex-col sm:flex-row items-start sm:items-center gap-5"
              >
                <a
                  href="#start"
                  className="group relative inline-flex items-center gap-3 bg-foreground text-background px-8 py-4 font-mono text-[12px] uppercase tracking-[0.15em] overflow-hidden transition-all duration-500 hover:shadow-[0_20px_60px_-15px_hsl(225_45%_42%/0.3)]"
                >
                  <span className="relative z-10">Try PuzzleAI</span>
                  <svg width="14" height="14" viewBox="0 0 16 16" fill="none" className="relative z-10 transition-transform duration-300 group-hover:translate-x-1.5">
                    <path d="M3 8H13M13 8L9 4M13 8L9 12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
                  </svg>
                  <div className="absolute inset-0 bg-accent/30 translate-y-full group-hover:translate-y-0 transition-transform duration-500" />
                </a>
                <a
                  href="#how-it-works"
                  className="group font-mono text-[12px] uppercase tracking-[0.15em] text-muted-foreground hover:text-foreground transition-colors duration-300 flex items-center gap-2"
                >
                  <span className="border-b border-muted-foreground/30 pb-0.5 group-hover:border-accent transition-colors duration-300">See how it works</span>
                  <motion.span
                    animate={{ y: [0, 4, 0] }}
                    transition={{ repeat: Infinity, duration: 2, ease: "easeInOut" }}
                    className="text-accent/60"
                  >↓</motion.span>
                </a>
              </motion.div>
            </div>

            {/* Right side: spacer for crystal (crystal is position:absolute centered) */}
            <div className="hidden md:block" />
          </div>
        </div>

        {/* ── Bottom metrics strip ── */}
        <div className="max-w-[1400px] mx-auto w-full px-8">
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            transition={{ delay: 2.5, duration: 1.2 }}
            className="pt-6 border-t border-border/50 flex flex-wrap items-end justify-between"
          >
            <div className="flex gap-10 md:gap-16">
              {[
                { num: "01", label: "Performance", desc: "use case success rate" },
                { num: "02", label: "Speed", desc: "real-world latency" },
                { num: "03", label: "Cost", desc: "actual token pricing" },
              ].map((m, i) => (
                <motion.div
                  key={m.label}
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: 2.8 + i * 0.15, duration: 0.5 }}
                  className="group cursor-default"
                >
                  <span className="font-mono text-[8px] text-accent/30 block mb-1">{m.num}</span>
                  <span className="font-grotesk font-semibold text-xs tracking-tight group-hover:text-accent transition-colors duration-500">{m.label}</span>
                  <span className="hidden md:block font-mono text-[9px] text-muted-foreground/35 mt-0.5">{m.desc}</span>
                </motion.div>
              ))}
            </div>

            <motion.div
              initial={{ opacity: 0, scale: 0.9 }}
              animate={{ opacity: 1, scale: 1 }}
              transition={{ delay: 3, duration: 0.6 }}
              className="hidden md:flex items-center gap-3 border border-border/50 bg-background/60 backdrop-blur-sm px-4 py-2.5"
            >
              <motion.div
                className="w-1.5 h-1.5 bg-accent"
                animate={{ opacity: [0.3, 1, 0.3] }}
                transition={{ repeat: Infinity, duration: 2 }}
              />
              <span className="font-mono text-[9px] text-muted-foreground/60 tracking-wider">4,291 evaluations completed</span>
            </motion.div>
          </motion.div>
        </div>
      </div>
    </section>
  );
};

export default Hero;
