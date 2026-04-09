import { motion } from "framer-motion";

const WhySection = () => {
  return (
    <section id="about" className="py-32 relative">
      {/* Diagonal line texture — different from HowItWorks angle */}
      <div
        className="absolute inset-0 opacity-[0.02]"
        style={{
          backgroundImage: `repeating-linear-gradient(
            45deg,
            transparent,
            transparent 100px,
            hsl(var(--foreground)) 100px,
            hsl(var(--foreground)) 101px
          )`,
        }}
      />
      <div className="absolute inset-0 bg-gradient-to-b from-background via-card/20 to-background" />

      <div className="max-w-[1400px] mx-auto px-8 relative">
        <motion.div
          initial={{ opacity: 0, y: 30 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, margin: "-100px" }}
          transition={{ duration: 0.6 }}
          className="mb-24"
        >
          <div className="mb-8">
            <span className="font-grotesk font-semibold text-[11px] uppercase tracking-[0.25em] text-accent/50 block">
              Why PuzzleAI
            </span>
            <div className="w-8 h-[2px] bg-accent/30 mt-3" style={{ transform: "skewX(-20deg)" }} />
          </div>
          <h2 className="font-display text-[clamp(2.5rem,5vw,4.5rem)] leading-[1] tracking-[-0.02em] max-w-3xl">
            The AI landscape is overwhelming.
            <br />
            <span className="italic text-muted-foreground">We make it navigable.</span>
          </h2>
        </motion.div>

        {/* Asymmetric staggered grid — NOT a standard bento */}
        <div className="grid md:grid-cols-12 gap-4">
          {/* Hero card — offset with a left margin for asymmetry */}
          <motion.div
            initial={{ opacity: 0, y: 30 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true }}
            transition={{ duration: 0.6 }}
            className="md:col-start-1 md:col-span-7 border border-border p-10 md:p-14 relative group overflow-hidden cursor-default bg-background/50 backdrop-blur-sm"
          >
            {/* Diagonal corner accent — signature motif */}
            <div className="absolute top-0 right-0 w-16 h-16 overflow-hidden">
              <div className="absolute top-0 right-0 w-[1px] h-24 bg-accent/20 origin-top-right" style={{ transform: "rotate(-45deg) translateX(50%)" }} />
            </div>
            <div className="absolute bottom-0 left-0 w-0 h-[2px] bg-accent/40 group-hover:w-[40%] transition-all duration-700" style={{ transform: "skewX(-20deg)" }} />

            <div className="relative z-10">
              <span className="font-grotesk font-semibold text-[10px] uppercase tracking-[0.25em] text-accent/40 block mb-6">
                Real testing
              </span>
              <h3 className="font-display text-3xl md:text-5xl leading-[1.05] mb-4 group-hover:translate-x-1 transition-transform duration-500">
                We don't aggregate reviews.
                <br />We run your <span className="italic">actual</span> workload.
              </h3>
              <p className="text-muted-foreground max-w-lg text-[15px] leading-relaxed">
                Every AI solution is tested in a sandboxed environment against your actual tasks and data. 
                You see what works, what breaks, and what wins before you commit.
              </p>

              <motion.div
                initial={{ opacity: 0, y: 20 }}
                whileInView={{ opacity: 1, y: 0 }}
                viewport={{ once: true }}
                transition={{ delay: 0.4 }}
                className="mt-10 flex flex-col sm:flex-row gap-3"
              >
                <div className="border border-border px-5 py-4 bg-background/80 flex-1">
                  <span className="font-grotesk font-semibold text-[9px] uppercase tracking-[0.2em] text-muted-foreground/40 block mb-2">Without Puzzle</span>
                  <p className="font-display text-[15px] leading-snug text-muted-foreground">
                    3 weeks of trial-and-error<br />across 4 tools
                  </p>
                </div>
                <div className="border border-accent/30 px-5 py-4 bg-accent/[0.04] flex-1">
                  <span className="font-grotesk font-semibold text-[9px] uppercase tracking-[0.2em] text-accent/60 block mb-2">With Puzzle</span>
                  <p className="font-display text-[15px] leading-snug">
                    5 minutes, side-by-side,<br />with your data
                  </p>
                </div>
              </motion.div>
            </div>
          </motion.div>

          {/* Right column — offset downward for stagger */}
          <div className="md:col-start-8 md:col-span-5 flex flex-col gap-4 md:mt-12">
            <motion.div
              initial={{ opacity: 0, y: 30 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true }}
              transition={{ duration: 0.6, delay: 0.1 }}
              className="flex-1 border border-border p-8 flex flex-col justify-between group cursor-default hover:border-accent/30 transition-all duration-500 bg-background/50 backdrop-blur-sm relative overflow-hidden"
            >
              <div className="absolute bottom-0 left-0 w-0 h-[2px] bg-accent/40 group-hover:w-[40%] transition-all duration-700" style={{ transform: "skewX(-20deg)" }} />
              <div>
                <span className="font-grotesk font-semibold text-[10px] uppercase tracking-[0.25em] text-accent/40 block mb-4">Speed</span>
                <h3 className="font-display text-2xl md:text-3xl leading-tight mb-3 group-hover:translate-x-1 transition-transform duration-500">
                  Results in under<br /><span className="italic">five minutes.</span>
                </h3>
              </div>
              <p className="text-muted-foreground text-sm mt-4">
                What used to take weeks of manual research now takes a single conversation.
              </p>
            </motion.div>

            <motion.div
              initial={{ opacity: 0, y: 30 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true }}
              transition={{ duration: 0.6, delay: 0.2 }}
              className="flex-1 border border-border p-8 flex flex-col justify-between group cursor-default hover:border-accent/30 transition-all duration-500 bg-background/50 backdrop-blur-sm relative overflow-hidden"
            >
              <div className="absolute bottom-0 left-0 w-0 h-[2px] bg-accent/40 group-hover:w-[40%] transition-all duration-700" style={{ transform: "skewX(-20deg)" }} />
              <div>
                <span className="font-grotesk font-semibold text-[10px] uppercase tracking-[0.25em] text-accent/40 block mb-4">Transparency</span>
                <h3 className="font-display text-2xl md:text-3xl leading-tight mb-3 group-hover:translate-x-1 transition-transform duration-500">
                  Three metrics.<br /><span className="italic">Zero noise.</span>
                </h3>
              </div>
              <p className="text-muted-foreground text-sm mt-4">
                Performance, speed, and cost. We cut everything else so you can make confident decisions.
              </p>
            </motion.div>
          </div>

          {/* Full-width bottom row with asymmetric internal layout */}
          <motion.div
            initial={{ opacity: 0, y: 30 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true }}
            transition={{ duration: 0.6, delay: 0.3 }}
            className="md:col-span-12 border border-border p-8 md:p-10 bg-background/50 backdrop-blur-sm relative overflow-hidden group"
          >
            <div className="absolute bottom-0 left-0 w-0 h-[2px] bg-accent/40 group-hover:w-[25%] transition-all duration-1000" style={{ transform: "skewX(-20deg)" }} />
            <div className="grid md:grid-cols-3 gap-8 md:gap-16">
              {[
                {
                  num: "01",
                  title: "200+ solutions indexed",
                  desc: "From frontier models to specialized agents. Every major provider, continuously updated.",
                },
                {
                  num: "02",
                  title: "Synthetic test generation",
                  desc: "We don't just use your data — we synthesize edge cases, adversarial inputs, and scaling scenarios.",
                },
                {
                  num: "03",
                  title: "Real-time evaluation",
                  desc: "Watch tests run live. No waiting for reports — see every result as it happens.",
                },
              ].map((item, i) => (
                <div key={i} className="flex gap-4">
                  <span className="font-grotesk font-bold text-[11px] text-accent/20 flex-shrink-0 mt-0.5">{item.num}</span>
                  <div>
                    <h4 className="font-grotesk font-semibold text-sm mb-2">{item.title}</h4>
                    <p className="text-muted-foreground text-sm leading-relaxed">{item.desc}</p>
                  </div>
                </div>
              ))}
            </div>
          </motion.div>
        </div>
      </div>
    </section>
  );
};

export default WhySection;
