import { motion } from "framer-motion";
import gradientMesh from "@/assets/gradient-mesh.jpg";

const WhySection = () => {
  return (
    <section id="about" className="py-32">
      <div className="max-w-[1400px] mx-auto px-8">
        <motion.div
          initial={{ opacity: 0, y: 30 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, margin: "-100px" }}
          transition={{ duration: 0.6 }}
          className="mb-24"
        >
          <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-muted-foreground block mb-6">
            Why PuzzleAI
          </span>
          <h2 className="font-display text-[clamp(2.5rem,5vw,4.5rem)] leading-[1] tracking-[-0.02em] max-w-3xl">
            The AI landscape is overwhelming.
            <br />
            <span className="italic text-muted-foreground">We make it navigable.</span>
          </h2>
        </motion.div>

        {/* Editorial grid — asymmetric like a magazine */}
        <div className="grid md:grid-cols-12 gap-6">
          {/* Large feature card */}
          <motion.div
            initial={{ opacity: 0, y: 30 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true }}
            transition={{ duration: 0.6 }}
            className="md:col-span-7 relative overflow-hidden group"
          >
            <div className="aspect-[4/3] relative overflow-hidden bg-foreground">
              <img
                src={gradientMesh}
                alt=""
                loading="lazy"
                width={1920}
                height={800}
                className="w-full h-full object-cover opacity-60 group-hover:scale-105 transition-transform duration-700"
              />
              <div className="absolute inset-0 bg-gradient-to-t from-foreground via-foreground/50 to-transparent" />
              <div className="absolute bottom-0 left-0 right-0 p-10 text-background">
                <span className="font-mono text-[10px] uppercase tracking-[0.2em] text-background/40 block mb-3">
                  Real testing
                </span>
                <h3 className="font-display text-3xl md:text-4xl leading-tight mb-3">
                  We don't aggregate reviews.
                  <br />We run your <span className="italic">actual</span> workload.
                </h3>
                <p className="text-background/50 max-w-md">
                  Every AI solution is tested against synthesized scenarios built from your real data and workflows.
                </p>
              </div>
            </div>
          </motion.div>

          {/* Right column — stacked cards */}
          <div className="md:col-span-5 flex flex-col gap-6">
            <motion.div
              initial={{ opacity: 0, y: 30 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true }}
              transition={{ duration: 0.6, delay: 0.1 }}
              className="flex-1 border border-border p-10 flex flex-col justify-end group cursor-default hover:bg-secondary/50 transition-colors duration-500"
            >
              <span className="font-mono text-[10px] uppercase tracking-[0.2em] text-muted-foreground block mb-4">
                Speed
              </span>
              <h3 className="font-display text-3xl leading-tight mb-3 group-hover:translate-x-1 transition-transform duration-500">
                Results in under
                <br /><span className="italic">five minutes.</span>
              </h3>
              <p className="text-muted-foreground text-sm">
                What used to take weeks of manual research now takes a single conversation.
              </p>
            </motion.div>

            <motion.div
              initial={{ opacity: 0, y: 30 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true }}
              transition={{ duration: 0.6, delay: 0.2 }}
              className="flex-1 border border-border p-10 flex flex-col justify-end group cursor-default hover:bg-secondary/50 transition-colors duration-500"
            >
              <span className="font-mono text-[10px] uppercase tracking-[0.2em] text-muted-foreground block mb-4">
                Transparency
              </span>
              <h3 className="font-display text-3xl leading-tight mb-3 group-hover:translate-x-1 transition-transform duration-500">
                Three metrics.
                <br /><span className="italic">Zero noise.</span>
              </h3>
              <p className="text-muted-foreground text-sm">
                Performance, speed, and cost. We cut the BS so you can make confident decisions.
              </p>
            </motion.div>
          </div>
        </div>
      </div>
    </section>
  );
};

export default WhySection;
