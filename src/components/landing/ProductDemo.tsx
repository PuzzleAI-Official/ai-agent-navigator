import { motion, useInView } from "framer-motion";
import { useRef, useState, useEffect } from "react";

const AnimatedCounter = ({ target, suffix = "" }: { target: number; suffix?: string }) => {
  const ref = useRef<HTMLSpanElement>(null);
  const isInView = useInView(ref, { once: true });
  const [count, setCount] = useState(0);

  useEffect(() => {
    if (!isInView) return;
    let start = 0;
    const duration = 1500;
    const startTime = Date.now();
    const animate = () => {
      const elapsed = Date.now() - startTime;
      const progress = Math.min(elapsed / duration, 1);
      const eased = 1 - Math.pow(1 - progress, 3);
      setCount(Math.round(eased * target));
      if (progress < 1) requestAnimationFrame(animate);
    };
    requestAnimationFrame(animate);
  }, [isInView, target]);

  return <span ref={ref}>{count}{suffix}</span>;
};

const ProductDemo = () => {
  const [activeProvider, setActiveProvider] = useState(0);

  const providers = [
    { name: "GPT-4o", perf: 94, speed: 1.2, cost: 0.003, verdict: "Best overall" },
    { name: "Claude 3.5", perf: 91, speed: 0.9, cost: 0.005, verdict: "Fastest" },
    { name: "Gemini Pro", perf: 87, speed: 1.8, cost: 0.001, verdict: "Most affordable" },
  ];

  return (
    <section className="py-32 bg-foreground text-background overflow-hidden">
      <div className="max-w-[1400px] mx-auto px-8">
        <div className="grid md:grid-cols-2 gap-16 items-center">
          {/* Left — editorial text */}
          <motion.div
            initial={{ opacity: 0, y: 30 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true, margin: "-100px" }}
            transition={{ duration: 0.6 }}
          >
            <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-background/40 block mb-6">
              Live evaluation
            </span>
            <h2 className="font-display text-[clamp(2rem,4vw,3.5rem)] leading-[1.05] tracking-[-0.02em] mb-6">
              See results in
              <br />
              <span className="italic">real time.</span>
            </h2>
            <p className="text-background/50 leading-relaxed max-w-md mb-10">
              Watch as PuzzleAI tests each provider against your specific use cases. 
              No black boxes — you see every test, every result, every cost.
            </p>

            {/* Stats */}
            <div className="grid grid-cols-3 gap-8">
              {[
                { val: 200, suffix: "+", label: "Solutions" },
                { val: 50, suffix: "k+", label: "Tests run" },
                { val: 5, suffix: "min", label: "Avg. time" },
              ].map((s) => (
                <div key={s.label}>
                  <div className="font-display text-4xl md:text-5xl tracking-tight">
                    <AnimatedCounter target={s.val} suffix={s.suffix} />
                  </div>
                  <div className="font-mono text-[10px] uppercase tracking-[0.15em] text-background/30 mt-1">{s.label}</div>
                </div>
              ))}
            </div>
          </motion.div>

          {/* Right — interactive result cards */}
          <motion.div
            initial={{ opacity: 0, x: 40 }}
            whileInView={{ opacity: 1, x: 0 }}
            viewport={{ once: true, margin: "-100px" }}
            transition={{ duration: 0.7, delay: 0.2 }}
            className="space-y-4"
          >
            {providers.map((p, i) => (
              <motion.div
                key={p.name}
                onMouseEnter={() => setActiveProvider(i)}
                className={`border p-6 transition-all duration-500 cursor-pointer ${
                  activeProvider === i
                    ? "border-background/30 bg-background/[0.06]"
                    : "border-background/10 bg-transparent hover:border-background/20"
                }`}
              >
                <div className="flex items-center justify-between mb-4">
                  <div>
                    <span className="font-grotesk font-semibold text-lg">{p.name}</span>
                    {activeProvider === i && (
                      <motion.span
                        initial={{ opacity: 0, x: -10 }}
                        animate={{ opacity: 1, x: 0 }}
                        className="ml-3 font-mono text-[10px] uppercase tracking-wider text-accent"
                      >
                        {p.verdict}
                      </motion.span>
                    )}
                  </div>
                  <span className="font-display text-3xl">{p.perf}%</span>
                </div>

                {/* Performance bar */}
                <div className="h-[2px] bg-background/10 mb-4 overflow-hidden">
                  <motion.div
                    className="h-full bg-accent"
                    initial={{ width: 0 }}
                    whileInView={{ width: `${p.perf}%` }}
                    viewport={{ once: true }}
                    transition={{ duration: 1, delay: 0.5 + i * 0.2, ease: "easeOut" }}
                  />
                </div>

                {/* Details row */}
                <div className="flex gap-8 font-mono text-[11px] tracking-wider text-background/40">
                  <span>
                    <span className="text-background/60">{p.speed}s</span> latency
                  </span>
                  <span>
                    <span className="text-background/60">${p.cost}</span> /request
                  </span>
                </div>
              </motion.div>
            ))}
          </motion.div>
        </div>
      </div>
    </section>
  );
};

export default ProductDemo;
