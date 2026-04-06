import Navbar from "@/components/landing/Navbar";
import Footer from "@/components/landing/Footer";

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

          <div className="border border-border p-10 md:p-14">
            <p className="text-[15px] md:text-[17px] text-foreground/90 leading-[1.8] max-w-[640px]">
              A2A-compatible. Agent Card discovery, sandbox-as-a-service, and evaluation 
              endpoints — designed for a world where agents choose their own tools.
            </p>

            <div className="mt-10 grid grid-cols-1 md:grid-cols-3 gap-8">
              <div>
                <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-muted-foreground">Agent Card Discovery</span>
                <p className="mt-3 text-[14px] text-muted-foreground/70 leading-relaxed">
                  Publish and discover agent capabilities through a standardized card format.
                </p>
              </div>
              <div>
                <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-muted-foreground">Sandbox-as-a-Service</span>
                <p className="mt-3 text-[14px] text-muted-foreground/70 leading-relaxed">
                  Spin up isolated environments to test candidates against your real workflows.
                </p>
              </div>
              <div>
                <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-muted-foreground">Evaluation Endpoints</span>
                <p className="mt-3 text-[14px] text-muted-foreground/70 leading-relaxed">
                  Run benchmarks programmatically and retrieve structured performance verdicts.
                </p>
              </div>
            </div>

            <div className="mt-12 pt-8 border-t border-border">
              <span className="font-mono text-[11px] uppercase tracking-[0.2em] text-muted-foreground">
                Coming soon — join the waitlist for early access
              </span>
            </div>
          </div>
        </div>
      </div>
      <Footer />
    </div>
  );
};

export default ApiDocs;
