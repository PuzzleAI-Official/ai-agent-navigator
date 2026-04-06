import { motion } from "framer-motion";
import Navbar from "@/components/landing/Navbar";
import Footer from "@/components/landing/Footer";
import heroSilk from "@/assets/hero-silk.png";

const About = () => {
  return (
    <div className="min-h-screen bg-background">
      <Navbar />

      {/* Silk visual — full bleed, cinematic */}
      <div className="relative w-full h-[60vh] md:h-[70vh] overflow-hidden">
        <motion.img
          src={heroSilk}
          alt="Abstract silk form"
          initial={{ opacity: 0, scale: 1.05 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ duration: 1.2, ease: "easeOut" }}
          className="w-full h-full object-cover object-center"
        />
        <div className="absolute inset-0 bg-gradient-to-b from-background/30 via-transparent to-background" />
      </div>

      {/* Memo section */}
      <section className="max-w-[680px] mx-auto px-8 py-24 md:py-32">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.7, delay: 0.3 }}
        >
          <span className="font-grotesk font-semibold text-[11px] uppercase tracking-[0.25em] text-accent/50 block mb-3">
            Founder's Memo
          </span>
          <div className="w-8 h-[2px] bg-accent/30 mb-12" style={{ transform: "skewX(-20deg)" }} />

          <h1 className="font-display text-[clamp(2.2rem,4.5vw,3.5rem)] leading-[1.1] tracking-[-0.02em] mb-12">
            Why we're building{" "}
            <span className="italic text-muted-foreground">this.</span>
          </h1>

          {/* Placeholder for memo content */}
          <div className="space-y-6 text-muted-foreground text-[16px] leading-[1.85] font-sans">
            <p className="text-foreground/30 italic font-display text-lg">
              Coming soon — the story behind PuzzleAI, in the founder's own words.
            </p>
          </div>
        </motion.div>
      </section>

      <Footer />
    </div>
  );
};

export default About;
