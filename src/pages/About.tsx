import { motion } from "framer-motion";
import Navbar from "@/components/landing/Navbar";
import Footer from "@/components/landing/Footer";
import heroSilk from "@/assets/hero-silk.png";

const About = () => {
  return (
    <div className="min-h-screen bg-background">
      <Navbar />

      {/* Silk visual — full bleed, cinematic with gentle float */}
      <div className="relative w-full h-[50vh] md:h-[60vh] overflow-hidden">
        <motion.img
          src={heroSilk}
          alt="Abstract silk form"
          initial={{ opacity: 0, scale: 1.08 }}
          animate={{ opacity: 1, scale: 1 }}
          transition={{ duration: 1.4, ease: "easeOut" }}
          className="w-full h-full object-cover object-center animate-float"
        />
        <div className="absolute inset-0 bg-gradient-to-b from-background/30 via-transparent to-background" />
      </div>

      {/* Memo section — left-aligned, pulled up */}
      <section className="max-w-[1400px] mx-auto px-8 -mt-16 relative z-10 pb-24 md:pb-32">
        <motion.div
          initial={{ opacity: 0, y: 20 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.7, delay: 0.3 }}
          className="max-w-[680px]"
        >
          <span className="font-grotesk font-semibold text-[11px] uppercase tracking-[0.25em] text-accent/50 block mb-3">
            Founder's Memo
          </span>
          <div className="w-8 h-[2px] bg-accent/30 mb-12" style={{ transform: "skewX(-20deg)" }} />

          <h1 className="font-display text-[clamp(2.2rem,4.5vw,3.5rem)] leading-[1.1] tracking-[-0.02em] mb-12">
            Why we're building{" "}
            <span className="italic text-muted-foreground">this.</span>
          </h1>

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
