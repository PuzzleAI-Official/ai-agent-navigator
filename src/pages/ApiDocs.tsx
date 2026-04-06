import Navbar from "@/components/landing/Navbar";
import Footer from "@/components/landing/Footer";
import ApiTypingDemo from "@/components/landing/ApiTypingDemo";

const ApiDocs = () => {
  return (
    <div className="min-h-screen bg-background">
      <Navbar />
      <div className="pt-32 pb-20 px-8">
        <div className="max-w-[1400px] mx-auto">
          <div className="flex items-center gap-4 mb-8">
            <div className="w-8 h-[2px] bg-accent/30" style={{ transform: "skewX(-20deg)" }} />
            <span className="font-grotesk font-semibold text-[11px] uppercase tracking-[0.25em] text-muted-foreground">
              Built for the Agent Era
            </span>
          </div>

          <h1 className="font-display text-[clamp(2.5rem,5vw,5rem)] leading-[0.95] tracking-[-0.03em] text-foreground mb-6 font-serif">
            API Documentation
          </h1>
          <p className="text-[15px] md:text-[17px] text-muted-foreground max-w-[640px] leading-[1.8] mb-16">
            Integrate PuzzleAI into your workflow programmatically. Evaluate AI candidates, 
            run benchmarks, and get results — all via a simple REST API.
          </p>

          <ApiTypingDemo />
        </div>
      </div>
      <Footer />
    </div>
  );
};

export default ApiDocs;
