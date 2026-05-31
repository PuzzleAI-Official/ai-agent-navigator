import { Link } from "react-router-dom";
import ApiTypingDemo from "@/components/landing/ApiTypingDemo";
import AgentRoutingDirectory from "@/components/landing/AgentRoutingDirectory";
import Footer from "@/components/landing/Footer";
import Navbar from "@/components/landing/Navbar";

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
          <p className="text-[15px] md:text-[17px] text-muted-foreground max-w-[640px] leading-[1.8] mb-8">
            Integrate PuzzleAI into your workflow programmatically. Evaluate AI candidates, 
            run benchmarks, and get results — all via a simple REST API.
          </p>

          <div className="flex flex-wrap gap-3 mb-16">
            <Link
              to="/alpha-api"
              className="inline-flex items-center justify-center border border-foreground bg-foreground px-5 py-3 font-grotesk text-[12px] uppercase tracking-[0.16em] text-background hover:bg-accent hover:border-accent transition-colors"
              aria-label="Open the full Puzzle Alpha API reference documentation"
            >
              Open API reference
            </Link>
          </div>

          <ApiTypingDemo />
          <AgentRoutingDirectory />
        </div>
      </div>
      <Footer />
    </div>
  );
};

export default ApiDocs;
