import { motion, useMotionValue, useTransform, useSpring } from "framer-motion";
import { Button } from "@/components/ui/button";
import { ArrowRight, Search, Zap, BarChart3 } from "lucide-react";
import { useRef } from "react";

const ProductPreview = () => {
  return (
    <div className="relative w-full max-w-3xl mx-auto mt-16">
      {/* Glow behind card */}
      <div className="absolute -inset-4 bg-gradient-brand opacity-[0.06] blur-3xl rounded-3xl pointer-events-none" />

      <div className="relative rounded-2xl border border-border bg-card shadow-2xl overflow-hidden glow-subtle">
        {/* Browser chrome */}
        <div className="flex items-center gap-2 px-4 py-3 border-b border-border bg-secondary/50">
          <div className="flex gap-1.5">
            <div className="h-2.5 w-2.5 rounded-full bg-muted-foreground/20" />
            <div className="h-2.5 w-2.5 rounded-full bg-muted-foreground/20" />
            <div className="h-2.5 w-2.5 rounded-full bg-muted-foreground/20" />
          </div>
          <div className="flex-1 flex justify-center">
            <div className="px-4 py-1 rounded-md bg-muted/80 text-xs text-muted-foreground font-mono">
              app.puzzleai.com
            </div>
          </div>
        </div>

        {/* Mock UI */}
        <div className="p-6 grid grid-cols-3 gap-4 min-h-[280px]">
          {/* Chat panel */}
          <div className="col-span-1 space-y-3">
            <div className="text-xs font-mono text-muted-foreground mb-2">Your request</div>
            <div className="rounded-lg bg-accent/60 p-3 text-xs text-foreground leading-relaxed">
              "I need an AI that can process customer support emails, categorize them, and draft responses..."
            </div>
            <div className="rounded-lg bg-muted/50 p-3 text-xs text-muted-foreground">
              <div className="flex items-center gap-2">
                <div className="h-1.5 w-1.5 rounded-full bg-primary animate-pulse" />
                Analyzing 12 candidates...
              </div>
            </div>
          </div>

          {/* Results panel */}
          <div className="col-span-2 space-y-3">
            <div className="text-xs font-mono text-muted-foreground mb-2">Evaluation results</div>

            {[
              { name: "Provider A", perf: 94, speed: "1.2s", cost: "$0.003", bar: "w-[94%]" },
              { name: "Provider B", perf: 87, speed: "0.8s", cost: "$0.008", bar: "w-[87%]" },
              { name: "Provider C", perf: 72, speed: "0.4s", cost: "$0.001", bar: "w-[72%]" },
            ].map((r, i) => (
              <motion.div
                key={r.name}
                initial={{ opacity: 0, x: 20 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ delay: 0.8 + i * 0.2, duration: 0.4 }}
                className="rounded-lg border border-border bg-card p-3 flex items-center gap-4"
              >
                <div className="flex-1">
                  <div className="flex items-center justify-between mb-1.5">
                    <span className="text-xs font-medium text-foreground">{r.name}</span>
                    <span className="text-xs font-mono text-primary font-semibold">{r.perf}%</span>
                  </div>
                  <div className="h-1.5 rounded-full bg-muted overflow-hidden">
                    <motion.div
                      initial={{ width: 0 }}
                      animate={{ width: `${r.perf}%` }}
                      transition={{ delay: 1 + i * 0.2, duration: 0.8, ease: "easeOut" }}
                      className="h-full rounded-full bg-gradient-brand"
                    />
                  </div>
                </div>
                <div className="text-[10px] font-mono text-muted-foreground text-right leading-tight">
                  <div>{r.speed}</div>
                  <div>{r.cost}/req</div>
                </div>
              </motion.div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
};

const Hero = () => {
  return (
    <section className="relative min-h-screen flex flex-col items-center justify-center overflow-hidden pt-16 pb-20">
      {/* Animated grid */}
      <div className="absolute inset-0 animated-grid" />

      {/* Radial gradient overlay */}
      <div className="absolute inset-0 bg-gradient-to-b from-background via-transparent to-background pointer-events-none" />

      {/* Floating orbs */}
      <div className="absolute top-1/4 right-1/4 w-72 h-72 rounded-full bg-primary/5 blur-[100px] pointer-events-none" style={{ animation: "float 6s ease-in-out infinite" }} />
      <div className="absolute bottom-1/3 left-1/4 w-56 h-56 rounded-full bg-[hsl(200_80%_55%_/_0.05)] blur-[80px] pointer-events-none" style={{ animation: "float 8s ease-in-out infinite 1s" }} />

      <div className="container relative z-10 text-center max-w-5xl mx-auto px-4">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, delay: 0.1 }}
          className="mb-8"
        >
          <span className="inline-flex items-center gap-2.5 rounded-full border border-primary/20 bg-accent/80 backdrop-blur-sm px-4 py-1.5 text-xs font-mono text-accent-foreground">
            <span className="h-1.5 w-1.5 rounded-full bg-primary animate-pulse" />
            The first agent-to-agent hiring platform
          </span>
        </motion.div>

        <motion.h1
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, delay: 0.2 }}
          className="text-5xl md:text-7xl lg:text-[5.5rem] font-heading font-bold tracking-tight leading-[1.05] mb-6"
        >
          Stop guessing.
          <br />
          <span className="text-gradient">Hire the right AI.</span>
        </motion.h1>

        <motion.p
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, delay: 0.3 }}
          className="text-lg md:text-xl text-muted-foreground max-w-2xl mx-auto mb-10 leading-relaxed font-body"
        >
          Describe your workflow. We test every AI solution against your real use cases
          and give you three numbers:{" "}
          <span className="text-foreground font-medium">performance</span>,{" "}
          <span className="text-foreground font-medium">speed</span>,{" "}
          <span className="text-foreground font-medium">cost</span>.
        </motion.p>

        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.5, delay: 0.4 }}
          className="flex flex-col sm:flex-row items-center justify-center gap-4"
        >
          <Button
            size="lg"
            className="bg-gradient-brand text-primary-foreground hover:opacity-90 transition-opacity font-medium text-base px-8 h-12 gap-2 group shadow-lg shadow-primary/20"
          >
            Find your AI
            <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-1" />
          </Button>
          <Button
            variant="outline"
            size="lg"
            className="border-border text-foreground hover:bg-secondary h-12 px-8 text-base"
          >
            See how it works
          </Button>
        </motion.div>

        {/* Product preview */}
        <motion.div
          initial={{ opacity: 0, y: 40 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.8, delay: 0.6 }}
        >
          <ProductPreview />
        </motion.div>
      </div>
    </section>
  );
};

export default Hero;
