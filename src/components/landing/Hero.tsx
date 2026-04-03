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

  const offsetX = (mousePos.x - 0.5) * 25;
  const offsetY = (mousePos.y - 0.5) * 18;

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
      {/* ── Rich warm gradient background ── */}
      <div className="absolute inset-0" style={{
        background: "linear-gradient(165deg, hsl(38 45% 93%) 0%, hsl(40 33% 97%) 35%, hsl(42 30% 96%) 60%, hsl(38 35% 94%) 100%)"
      }} />
      
      {/* Warm radial glow behind crystal area */}
      <div className="absolute top-[-10%] left-[20%] w-[70%] h-[90%] bg-[radial-gradient(ellipse_at_55%_40%,hsl(35_55%_88%/0.6),transparent_65%)] pointer-events-none" />
      <div className="absolute top-[10%] left-[30%] w-[50%] h-[70%] bg-[radial-gradient(ellipse_at_50%_45%,hsl(270_30%_90%/0.2),transparent_55%)] pointer-events-none" />
      <div className="absolute bottom-0 left-0 w-full h-[40%] bg-gradient-to-t from-background to-transparent pointer-events-none" />

      {/* ── Crystal Centerpiece — LARGE and dominant ── */}
      <div className="absolute inset-0 flex items-start justify-center pointer-events-none pt-8 md:pt-0 md:items-center">
        {/* Soft glow halo */}
        <motion.div
          initial={{ opacity: 0, scale: 0.5 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ delay: 0.3, duration: 2.5, ease: [0.16, 1, 0.3, 1] }}
          className="absolute top-[8%] md:top-[5%] w-[80vw] h-[80vw] md:w-[55vw] md:h-[55vw] max-w-[800px] max-h-[800px]"
          style={{
            background: "radial-gradient(circle, hsl(270 35% 85% / 0.15) 0%, hsl(35 50% 88% / 0.12) 30%, hsl(225 40% 88% / 0.06) 55%, transparent 75%)",
            transform: `translate(${offsetX * 0.3}px, ${offsetY * 0.3}px)`,
          }}
        />

        {/* THE CRYSTAL — big, beautiful, central */}
        <motion.img
          src={heroCrystal}
          alt=""
          width={1920}
          height={1080}
          initial={{ opacity: 0, scale: 0.7, y: 60 }}
          animate={{ opacity: 1, scale: 1, y: 0 }}
          transition={{ duration: 2, delay: 0.2, ease: [0.16, 1, 0.3, 1] }}
          className="relative z-10 w-[85vw] md:w-[55vw] lg:w-[48vw] max-w-[750px] h-auto mt-16 md:mt-0"
          style={{
            transform: `translate(${offsetX}px, ${offsetY}px)`,
            transition: "transform 0.15s ease-out",
            filter: "drop-shadow(0 30px 80px rgba(120, 90, 170, 0.12)) drop-shadow(0 10px 30px rgba(100, 80, 140, 0.08))",
          }}
        />

        {/* Floating light particles around crystal */}
        {[...Array(12)].map((_, i) => {
          const angle = (i / 12) * Math.PI * 2;
          const radius = 180 + (i % 4) * 60;
          const size = i % 3 === 0 ? 3 : 1.5;
          return (
            <motion.div
              key={i}
              className="absolute rounded-full"
              style={{
                width: size,
                height: size,
                left: `calc(50% + ${Math.cos(angle) * radius}px)`,
                top: `calc(42% + ${Math.sin(angle) * radius}px)`,
                background: i % 4 === 0 ? "hsl(225, 45%, 65%)" : i % 3 === 0 ? "hsl(35, 50%, 70%)" : "hsl(270, 30%, 75%)",
              }}
              animate={{
                y: [0, -20 - i * 2, 0],
                opacity: [0.15, 0.45, 0.15],
                scale: [1, 1.8, 1],
              }}
              transition={{
                duration: 3 + i * 0.4,
                repeat: Infinity,
                delay: i * 0.3,
                ease: "easeInOut",
              }}
            />
          );
        })}
      </div>

      {/* ── Content Layer ── */}
      <div className="relative z-20 min-h-screen flex flex-col justify-between pb-12 md:pb-20">
        <div className="max-w-[1400px] mx-auto w-full px-8 flex-1 flex items-end md:items-center">
          <div className="w-full pt-[65vh] md:pt-0">
            {/* Eyebrow */}
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              transition={{ delay: 0.5, duration: 0.8 }}
              className="flex items-center gap-4 mb-8"
            >
              <motion.div
                className="w-12 h-px bg-gradient-to-r from-accent to-accent/10"
                initial={{ scaleX: 0 }}
                animate={{ scaleX: 1 }}
                transition={{ delay: 0.7, duration: 1 }}
                style={{ transformOrigin: "left" }}
              />
              <span className="font-mono text-[11px] uppercase tracking-[0.3em] text-muted-foreground">
                The AI hiring platform
              </span>
            </motion.div>

            {/* Headline */}
            <motion.h1
              initial={{ opacity: 0, y: 50 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 1.2, delay: 0.5, ease: [0.16, 1, 0.3, 1] }}
              className="font-display text-[clamp(3.5rem,9vw,8rem)] leading-[0.85] tracking-[-0.04em] text-foreground max-w-3xl"
            >
              Find the
              <br />
              <span className="italic text-gradient">right</span> AI.
            </motion.h1>

            {/* Sub text */}
            <motion.p
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.7, delay: 1.1 }}
              className="mt-8 text-[15px] md:text-[16px] text-muted-foreground max-w-[420px] leading-[1.75]"
            >
              {displayed}
              {started && displayed.length < fullText.length && (
                <span className="animate-pulse text-accent ml-0.5">|</span>
              )}
            </motion.p>

            {/* CTAs */}
            <motion.div
              initial={{ opacity: 0, y: 20 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.6, delay: 1.4 }}
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
        </div>

        {/* ── Bottom metrics ── */}
        <div className="max-w-[1400px] mx-auto w-full px-8 mt-8">
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
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
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
