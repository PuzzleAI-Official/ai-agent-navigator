const Footer = () => {
  return (
    <footer className="border-t border-border py-10">
      <div className="container flex flex-col md:flex-row items-center justify-between gap-4 text-sm text-muted-foreground">
        <div className="flex items-center gap-2">
          <div className="h-5 w-5 rounded bg-primary flex items-center justify-center">
            <span className="text-primary-foreground font-heading font-bold text-[10px]">P</span>
          </div>
          <span className="font-heading font-medium text-foreground">PuzzleAI</span>
        </div>
        <div className="flex gap-6 font-mono text-xs">
          <a href="#" className="hover:text-foreground transition-colors">Twitter</a>
          <a href="#" className="hover:text-foreground transition-colors">LinkedIn</a>
          <a href="#" className="hover:text-foreground transition-colors">Contact</a>
        </div>
        <span className="font-mono text-xs">© 2026 PuzzleAI</span>
      </div>
    </footer>
  );
};

export default Footer;
