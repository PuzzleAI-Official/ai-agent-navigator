const Footer = () => {
  return (
    <footer className="border-t border-border py-12">
      <div className="max-w-[1400px] mx-auto px-8 flex flex-col md:flex-row items-center justify-between gap-6">
        <span className="font-grotesk font-semibold text-lg text-foreground">PuzzleAI</span>

        <div className="flex gap-10">
          {["Twitter", "LinkedIn", "Contact"].map((item) => (
            <a
              key={item}
              href="#"
              className="font-mono text-[11px] uppercase tracking-[0.15em] text-muted-foreground hover:text-foreground transition-colors"
            >
              {item}
            </a>
          ))}
        </div>

        <span className="font-mono text-[11px] text-muted-foreground">© 2026 PuzzleAI Inc.</span>
      </div>
    </footer>
  );
};

export default Footer;
