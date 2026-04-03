import { motion } from "framer-motion";
import { Button } from "@/components/ui/button";
import { ArrowRight } from "lucide-react";

const CTASection = () => {
  return (
    <section className="py-32 relative">
      <div className="container max-w-3xl mx-auto px-4 text-center">
        <motion.div
          initial={{ opacity: 0, y: 30 }}
          whileInView={{ opacity: 1, y: 0 }}
          viewport={{ once: true, margin: "-100px" }}
          transition={{ duration: 0.6 }}
          className="relative"
        >
          {/* Glow */}
          <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 w-[400px] h-[200px] rounded-full bg-primary/5 blur-[100px] pointer-events-none" />

          <h2 className="text-4xl md:text-6xl font-heading font-bold tracking-tight mb-6 relative">
            Find the AI that
            <br />
            <span className="text-gradient">actually works.</span>
          </h2>
          <p className="text-muted-foreground text-lg mb-10 max-w-xl mx-auto">
            Stop wasting time and money on the wrong tools. Let PuzzleAI test, compare, and recommend — so you can decide with confidence.
          </p>
          <Button size="lg" className="bg-primary text-primary-foreground hover:bg-primary/90 font-medium text-base px-10 h-13 gap-2 group">
            Start for free
            <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-1" />
          </Button>
        </motion.div>
      </div>
    </section>
  );
};

export default CTASection;
