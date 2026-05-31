import { motion, useInView, AnimatePresence } from "framer-motion";
import { useRef, useState, useEffect } from "react";

const AnimatedCounter = ({ target, suffix = "" }: { target: number; suffix?: string }) => {
  const ref = useRef<HTMLSpanElement>(null);
  const isInView = useInView(ref, { once: true });
  const [count, setCount] = useState(0);

  useEffect(() => {
    if (!isInView) return;
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

const TestSimulation = () => {
  const [activeTest, setActiveTest] = useState(0);
  const tests = [
    { name: "Contract summarization", status: "passed", time: "0.8s" },
    { name: "Email intent classification", status: "passed", time: "0.3s" },
    { name: "Multi-doc Q&A extraction", status: "failed", time: "2.1s" },
    { name: "Code review suggestions", status: "passed", time: "1.4s" },
    { name: "Customer tone analysis", status: "passed", time: "0.5s" },
    { name: "Table data structuring", status: "running", time: "—" },
  ];

  useEffect(() => {
    const interval = setInterval(() => {
      setActiveTest((prev) => (prev + 1) % tests.length);
    }, 2000);
    return () => clearInterval(interval);
  }, [tests.length]);

  return (
    <div className="space-y-0">
      {tests.map((test, i) => (
        <motion.div
          key={test.name}
          initial={{ opacity: 0, x: -20 }}
          whileInView={{ opacity: 1, x: 0 }}
          viewport={{ once: true }}
          transition={{ delay: 0.5 + i * 0.1, duration: 0.4 }}
          className={`flex items-center justify-between py-3 border-b border-background/[0.06] transition-all duration-300 ${
            i === activeTest ? "bg-background/[0.04] px-2" : ""
          }`}
        >
          <div className="flex items-center gap-3">
            <div className={`w-1.5 h-1.5 ${
              test.status === "passed" ? "bg-accent" :
              test.status === "failed" ? "bg-destructive" :
              "bg-background/40 animate-pulse"
            }`} />
            <span className="font-mono text-[11px] text-background/60">{test.name}</span>
          </div>
          <span className="font-mono text-[10px] text-background/30">{test.time}</span>
        </motion.div>
      ))}
    </div>
  );
};

const ProductDemo = () => {
  const [activeProvider, setActiveProvider] = useState(0);

  const providers = [
    { name: "Claude 4.6 Sonnet", perf: 94, speed: 0.9, cost: 0.005, verdict: "Best overall", tests: "47/50" },
    { name: "GPT-5.4", perf: 89, speed: 1.2, cost: 0.003, verdict: "Best value", tests: "44/50" },
    { name: "Gemini 3.1 Pro", perf: 76, speed: 1.8, cost: 0.001, verdict: "Most affordable", tests: "38/50" },
  ];

  return (
    <section className="py-0">
      <div className="bg-foreground text-background relative overflow-hidden">
        <div
          className="absolute inset-0"
          style={{
            backgroundImage: "radial-gradient(circle at 1px 1px, hsl(40 33% 97% / 0.03) 1px, transparent 0)",
            backgroundSize: "40px 40px",
          }}
        />
        <div className="absolute top-0 right-0 w-[50%] h-[50%] bg-[radial-gradient(ellipse_at_80%_20%,hsl(225_45%_42%/0.08),transparent_60%)]" />
        <div className="absolute bottom-0 left-0 w-[40%] h-[40%] bg-[radial-gradient(ellipse_at_20%_80%,hsl(225_45%_42%/0.04),transparent_60%)]" />

        {/* Decorative cross markers */}
        <div className="absolute top-8 right-8 opacity-[0.08]">
          <div className="w-6 h-px bg-background" />
          <div className="w-px h-6 bg-background -mt-3 ml-[11px]" />
        </div>

        <div className="max-w-[1400px] mx-auto px-8 py-32 relative">
          <div className="grid md:grid-cols-12 gap-16">
            <motion.div
              className="md:col-span-5"
              initial={{ opacity: 0, y: 30 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true, margin: "-100px" }}
              transition={{ duration: 0.6 }}
            >
              <div className="flex items-center gap-4 mb-6">
                <motion.div
                  className="w-12 h-px bg-accent/40"
                  initial={{ scaleX: 0 }}
                  whileInView={{ scaleX: 1 }}
                  viewport={{ once: true }}
                  transition={{ duration: 0.6 }}
                  style={{ transformOrigin: "left" }}
                />
                <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-background/30">
                  Live evaluation
                </span>
              </div>
              <h2 className="font-display text-[clamp(2.2rem,4vw,4rem)] leading-[1] tracking-[-0.02em] mb-8">
                Your workflow,
                <br />
                <span className="italic text-background/50">benchmarked.</span>
              </h2>
              <p className="text-background/35 leading-relaxed max-w-md mb-12 text-[15px] whitespace-pre-line">
                We break your workflow into real-world scenarios, generate test data that matches your edge cases, and run every candidate head-to-head in a sandbox — with your data, not theirs.
                {"\n"}Every result is transparent. Every comparison is earned.
              </p>

              <div className="grid grid-cols-3 gap-6 pt-8 border-t border-background/10">
                {[
                  { val: 200, suffix: "+", label: "AI solutions" },
                  { val: 50, suffix: "k", label: "Tests run" },
                  { val: 9, suffix: "min", label: "Avg. time" },
                ].map((s) => (
                  <div key={s.label}>
                    <div className="font-display text-3xl md:text-4xl tracking-tight">
                      <AnimatedCounter target={s.val} suffix={s.suffix} />
                    </div>
                    <div className="font-mono text-[9px] uppercase tracking-[0.15em] text-background/20 mt-1">{s.label}</div>
                  </div>
                ))}
              </div>
            </motion.div>

            <motion.div
              className="md:col-span-7"
              initial={{ opacity: 0, x: 40 }}
              whileInView={{ opacity: 1, x: 0 }}
              viewport={{ once: true, margin: "-100px" }}
              transition={{ duration: 0.7, delay: 0.2 }}
            >
              <div className="border border-background/10 overflow-hidden bg-background/[0.02] backdrop-blur-sm">
                <div className="flex items-center gap-2 px-4 py-3 border-b border-background/10">
                  <div className="w-2 h-2 bg-accent/60" />
                  <div className="w-2 h-2 bg-background/15" />
                  <div className="w-2 h-2 bg-background/10" />
                  <span className="ml-3 font-mono text-[10px] text-background/25 tracking-wider">puzzleai — evaluation running</span>
                </div>

                <div className="grid md:grid-cols-2">
                  <div className="p-6 border-r border-background/10">
                    <div className="font-mono text-[9px] uppercase tracking-[0.2em] text-background/20 mb-4">
                      Test scenarios
                    </div>
                    <TestSimulation />
                  </div>

                  <div className="p-6">
                    <div className="font-mono text-[9px] uppercase tracking-[0.2em] text-background/20 mb-4">
                      Verdicts
                    </div>
                    <div className="space-y-3">
                      {providers.map((p, i) => (
                        <motion.div
                          key={p.name}
                          onMouseEnter={() => setActiveProvider(i)}
                          className={`p-4 border transition-all duration-300 cursor-pointer ${
                            activeProvider === i
                              ? "border-accent/30 bg-accent/[0.06]"
                              : "border-background/10 hover:border-background/20"
                          }`}
                        >
                          <div className="flex items-center justify-between mb-3">
                            <span className="font-grotesk font-medium text-sm">{p.name}</span>
                            <span className="font-display text-2xl italic">{p.perf}%</span>
                          </div>
                          
                          <div className="h-[2px] bg-background/10 mb-3 overflow-hidden">
                            <motion.div
                              className="h-full bg-accent"
                              initial={{ width: 0 }}
                              whileInView={{ width: `${p.perf}%` }}
                              viewport={{ once: true }}
                              transition={{ duration: 1, delay: 0.8 + i * 0.2 }}
                            />
                          </div>

                          <AnimatePresence>
                            {activeProvider === i && (
                              <motion.div
                                initial={{ height: 0, opacity: 0 }}
                                animate={{ height: "auto", opacity: 1 }}
                                exit={{ height: 0, opacity: 0 }}
                                transition={{ duration: 0.3 }}
                                className="overflow-hidden"
                              >
                                <div className="flex gap-6 font-mono text-[10px] tracking-wider text-background/30 pt-2">
                                  <span><span className="text-background/50">{p.speed}s</span> latency</span>
                                  <span><span className="text-background/50">${p.cost}</span> /req</span>
                                  <span><span className="text-accent/80">{p.tests}</span> passed</span>
                                </div>
                              </motion.div>
                            )}
                          </AnimatePresence>
                        </motion.div>
                      ))}
                    </div>
                  </div>
                </div>
              </div>
            </motion.div>
          </div>
        </div>
      </div>
    </section>
  );
};

export default ProductDemo;
