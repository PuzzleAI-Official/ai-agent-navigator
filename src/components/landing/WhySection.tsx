import { motion } from "framer-motion";

const WhySection = () => {
  return (
    <section id="about" className="py-32 relative noise-overlay">
      <div className="absolute inset-0 bg-gradient-to-b from-background via-card/30 to-background" />
      <div
        className="absolute inset-0 opacity-[0.2]"
        style={{
          backgroundImage: "radial-gradient(circle at 1px 1px, hsl(var(--foreground) / 0.04) 1px, transparent 0)",
          backgroundSize: "48px 48px",
        }}
      />

      {/* Decorative corner marks */}
      <div className="absolute top-16 right-16 opacity-[0.06] hidden md:block">
        <div className="w-8 h-px bg-foreground" />
        <div className="w-px h-8 bg-foreground -mt-4 ml-[15px]" />
      </div>

      <div className="max-w-[1400px] mx-auto px-8 relative">
        <motion.div
          initial={{ opacity: 0, y: 30 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, margin: "-100px" }}
          transition={{ duration: 0.6 }}
          className="mb-24"
        >
          <div className="flex items-center gap-4 mb-6">
            <motion.div
              className="w-12 h-px bg-gradient-to-r from-accent to-accent/20"
              initial={{ scaleX: 0 }}
              whileInView={{ scaleX: 1 }}
              viewport={{ once: true }}
              transition={{ duration: 0.6 }}
              style={{ transformOrigin: "left" }}
            />
            <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-muted-foreground">
              Why PuzzleAI
            </span>
          </div>
          <h2 className="font-display text-[clamp(2.5rem,5vw,4.5rem)] leading-[1] tracking-[-0.02em] max-w-3xl">
            The AI landscape is overwhelming.
            <br />
            <span className="italic text-muted-foreground">We make it navigable.</span>
          </h2>
        </motion.div>

        <div className="grid md:grid-cols-12 gap-4">
          <motion.div
            initial={{ opacity: 0, y: 30 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true }}
            transition={{ duration: 0.6 }}
            className="md:col-span-8 border border-border p-10 md:p-14 relative group overflow-hidden cursor-default bg-background/50 backdrop-blur-sm"
          >
            <div className="absolute inset-0 opacity-0 group-hover:opacity-100 transition-opacity duration-700"
              style={{
                backgroundImage: "radial-gradient(circle at 1px 1px, hsl(var(--accent) / 0.06) 1px, transparent 0)",
                backgroundSize: "20px 20px",
              }}
            />
            <div className="absolute top-0 left-0 w-0 h-[2px] bg-gradient-to-r from-accent to-accent/30 group-hover:w-full transition-all duration-700" />

            <div className="relative z-10">
              <span className="font-mono text-[10px] uppercase tracking-[0.2em] text-accent/40 block mb-6">
                Real testing
              </span>
              <h3 className="font-display text-3xl md:text-5xl leading-[1.05] mb-4 group-hover:translate-x-1 transition-transform duration-500">
                We don't aggregate reviews.
                <br />We run your <span className="italic">actual</span> workload.
              </h3>
              <p className="text-muted-foreground max-w-lg text-[15px] leading-relaxed">
                Every AI solution is tested against synthesized scenarios built from your real data.
                We generate edge cases you haven't thought of, because the right solution must handle
                the unexpected.
              </p>

              <motion.div
                initial={{ opacity: 0, y: 20 }}
                whileInView={{ opacity: 1, y: 0 }}
                viewport={{ once: true }}
                transition={{ delay: 0.4 }}
                className="mt-10 inline-flex items-center gap-4 border border-accent/15 px-5 py-3 bg-accent/[0.03]"
              >
                <span className="font-display text-3xl text-accent italic">50k+</span>
                <span className="font-mono text-[10px] text-muted-foreground uppercase tracking-wider leading-tight">
                  test scenarios<br />generated
                </span>
              </motion.div>
            </div>
          </motion.div>

          <div className="md:col-span-4 flex flex-col gap-4">
            <motion.div
              initial={{ opacity: 0, y: 30 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true }}
              transition={{ duration: 0.6, delay: 0.1 }}
              className="flex-1 border border-border p-8 flex flex-col justify-between group cursor-default hover:border-accent/30 transition-all duration-500 bg-background/50 backdrop-blur-sm relative overflow-hidden"
            >
              <div className="absolute top-0 left-0 w-0 h-[2px] bg-gradient-to-r from-accent to-accent/30 group-hover:w-full transition-all duration-700" />
              <div>
                <span className="font-mono text-[10px] uppercase tracking-[0.2em] text-accent/40 block mb-4">Speed</span>
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
              <div className="absolute top-0 left-0 w-0 h-[2px] bg-gradient-to-r from-accent to-accent/30 group-hover:w-full transition-all duration-700" />
              <div>
                <span className="font-mono text-[10px] uppercase tracking-[0.2em] text-accent/40 block mb-4">Transparency</span>
                <h3 className="font-display text-2xl md:text-3xl leading-tight mb-3 group-hover:translate-x-1 transition-transform duration-500">
                  Three metrics.<br /><span className="italic">Zero noise.</span>
                </h3>
              </div>
              <p className="text-muted-foreground text-sm mt-4">
                Performance, speed, and cost. We cut everything else so you can make confident decisions.
              </p>
            </motion.div>
          </div>

          <motion.div
            initial={{ opacity: 0, y: 30 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true }}
            transition={{ duration: 0.6, delay: 0.3 }}
            className="md:col-span-12 border border-border p-8 md:p-10 bg-background/50 backdrop-blur-sm relative overflow-hidden group"
          >
            <div className="absolute top-0 left-0 w-0 h-[2px] bg-gradient-to-r from-accent to-accent/30 group-hover:w-full transition-all duration-1000" />
            <div className="grid md:grid-cols-3 gap-8 md:gap-16">
              {[
                {
                  icon: "◈",
                  title: "200+ solutions indexed",
                  desc: "From frontier models to specialized agents. Every major provider, continuously updated.",
                },
                {
                  icon: "◉",
                  title: "Synthetic test generation",
                  desc: "We don't just use your data — we synthesize edge cases, adversarial inputs, and scaling scenarios.",
                },
                {
                  icon: "◇",
                  title: "Real-time evaluation",
                  desc: "Watch tests run live. No waiting for reports — see every result as it happens.",
                },
              ].map((item, i) => (
                <div key={i} className="flex gap-4">
                  <span className="text-accent/30 text-lg flex-shrink-0 mt-0.5">{item.icon}</span>
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
