import { motion, useScroll, useTransform, MotionValue } from "framer-motion";
import { useRef } from "react";
import puzzlePiece1 from "@/assets/puzzle-piece-1.png";
import puzzlePiece2 from "@/assets/puzzle-piece-2.png";
import puzzlePiece3 from "@/assets/puzzle-piece-3.png";
import puzzlePiece4 from "@/assets/puzzle-piece-4.png";
import puzzlePiece6 from "@/assets/puzzle-piece-6.png";
import infinityAssembled from "@/assets/infinity-piece-6.png";

const images = [puzzlePiece1, puzzlePiece2, puzzlePiece3, puzzlePiece4, puzzlePiece6];

interface PieceConfig {
  src: string;
  startX: number;
  startY: number;
  startScale: number;
  startRotate: number;
  spinAmount: number;
  midX: number;
  midY: number;
  midScale: number;
  midRotate: number;
  enterAt: number;
  sizeClass: string;
}

// Pieces fly in → scatter → converge to center & fade out as infinity fades in
const pieces: PieceConfig[] = [
  {
    src: images[0],
    startX: 0, startY: 0, startScale: 3.2, startRotate: 0,
    spinAmount: 360,
    midX: 0, midY: 0, midScale: 0.38, midRotate: 0,
    enterAt: 0,
    sizeClass: "w-[50vw] md:w-[28vw] lg:w-[20vw] max-w-[280px]",
  },
  {
    src: images[1],
    startX: 800, startY: -400, startScale: 0.1, startRotate: -90,
    spinAmount: -540,
    midX: 160, midY: -40, midScale: 0.32, midRotate: 15,
    enterAt: 0.08,
    sizeClass: "w-[40vw] md:w-[24vw] lg:w-[17vw] max-w-[240px]",
  },
  {
    src: images[2],
    startX: -800, startY: -300, startScale: 0.1, startRotate: 120,
    spinAmount: 480,
    midX: -155, midY: -35, midScale: 0.30, midRotate: -20,
    enterAt: 0.1,
    sizeClass: "w-[38vw] md:w-[22vw] lg:w-[16vw] max-w-[220px]",
  },
  {
    src: images[3],
    startX: 600, startY: 500, startScale: 0.1, startRotate: 200,
    spinAmount: -420,
    midX: 120, midY: 55, midScale: 0.28, midRotate: 40,
    enterAt: 0.12,
    sizeClass: "w-[36vw] md:w-[20vw] lg:w-[15vw] max-w-[210px]",
  },
  {
    src: images[4],
    startX: -700, startY: 400, startScale: 0.1, startRotate: -150,
    spinAmount: 600,
    midX: -130, midY: 50, midScale: 0.26, midRotate: -35,
    enterAt: 0.14,
    sizeClass: "w-[34vw] md:w-[19vw] lg:w-[14vw] max-w-[200px]",
  },
  {
    src: images[0],
    startX: 1000, startY: 0, startScale: 0.05, startRotate: 45,
    spinAmount: -720,
    midX: 260, midY: 10, midScale: 0.22, midRotate: 60,
    enterAt: 0.18,
    sizeClass: "w-[30vw] md:w-[17vw] lg:w-[12vw] max-w-[170px]",
  },
  {
    src: images[1],
    startX: -900, startY: 100, startScale: 0.05, startRotate: -60,
    spinAmount: 540,
    midX: -250, midY: 15, midScale: 0.20, midRotate: -55,
    enterAt: 0.2,
    sizeClass: "w-[28vw] md:w-[16vw] lg:w-[11vw] max-w-[160px]",
  },
  {
    src: images[3],
    startX: 200, startY: -600, startScale: 0.05, startRotate: 180,
    spinAmount: -900,
    midX: 60, midY: -90, midScale: 0.18, midRotate: 25,
    enterAt: 0.22,
    sizeClass: "w-[24vw] md:w-[14vw] lg:w-[10vw] max-w-[140px]",
  },
  {
    src: images[4],
    startX: -300, startY: 600, startScale: 0.05, startRotate: -200,
    spinAmount: 720,
    midX: -50, midY: 85, midScale: 0.17, midRotate: -30,
    enterAt: 0.24,
    sizeClass: "w-[22vw] md:w-[13vw] lg:w-[9vw] max-w-[130px]",
  },
  {
    src: images[2],
    startX: 1200, startY: -200, startScale: 0.05, startRotate: 90,
    spinAmount: -480,
    midX: 320, midY: -20, midScale: 0.15, midRotate: 75,
    enterAt: 0.26,
    sizeClass: "w-[20vw] md:w-[12vw] lg:w-[8vw] max-w-[120px]",
  },
  {
    src: images[0],
    startX: -1100, startY: -150, startScale: 0.05, startRotate: -120,
    spinAmount: 600,
    midX: -310, midY: -25, midScale: 0.14, midRotate: -70,
    enterAt: 0.28,
    sizeClass: "w-[18vw] md:w-[11vw] lg:w-[8vw] max-w-[110px]",
  },
];

const ASSEMBLE_START = 0.65;
const ASSEMBLE_END = 0.85;

const PuzzleBackground = () => {
  const containerRef = useRef<HTMLDivElement>(null);
  const { scrollYProgress } = useScroll({
    target: containerRef,
    offset: ["start start", "end end"],
  });

  // Assembled infinity image fades in as pieces converge
  const infinityOpacity = useTransform(scrollYProgress, [ASSEMBLE_START, ASSEMBLE_END], [0, 1]);
  const infinityScale = useTransform(scrollYProgress, [ASSEMBLE_START, ASSEMBLE_END], [0.6, 1]);

  return (
    <div ref={containerRef} className="absolute inset-0 z-10 pointer-events-none">
      <div className="sticky top-0 h-screen flex items-center justify-center">
        {pieces.map((piece, i) => (
          <PuzzlePiece key={i} piece={piece} progress={scrollYProgress} />
        ))}
        {/* Assembled infinity symbol */}
        <motion.img
          src={infinityAssembled}
          alt=""
          width={512}
          height={512}
          className="absolute w-[70vw] md:w-[40vw] lg:w-[30vw] max-w-[480px] h-auto"
          style={{
            opacity: infinityOpacity,
            scale: infinityScale,
            filter: "drop-shadow(0 20px 50px rgba(80, 55, 30, 0.15))",
          }}
        />
      </div>
    </div>
  );
};

const PuzzlePiece = ({
  piece,
  progress,
}: {
  piece: PieceConfig;
  progress: MotionValue<number>;
}) => {
  const isCenter = piece.enterAt === 0;
  const enter = piece.enterAt;
  const scatterPoint = Math.min(enter + 0.3, 0.55);

  // Position: fly in → scatter → converge to center
  const x = useTransform(
    progress,
    [enter, scatterPoint, ASSEMBLE_START, ASSEMBLE_END],
    [piece.startX, piece.midX, piece.midX, 0]
  );
  const y = useTransform(
    progress,
    [enter, scatterPoint, ASSEMBLE_START, ASSEMBLE_END],
    [piece.startY, piece.midY, piece.midY, 0]
  );

  // Scale: shrink to nothing as they converge
  const scale = useTransform(
    progress,
    isCenter
      ? [0, 0.2, 0.5, ASSEMBLE_START, ASSEMBLE_END]
      : [enter, Math.min(enter + 0.15, 0.4), scatterPoint, ASSEMBLE_START, ASSEMBLE_END],
    isCenter
      ? [piece.startScale, piece.startScale * 0.7, piece.midScale * 1.3, piece.midScale, 0.05]
      : [piece.startScale, piece.midScale * 0.5, piece.midScale, piece.midScale, 0.05]
  );

  // Rotation
  const baseRotate = useTransform(
    progress,
    [enter, scatterPoint, ASSEMBLE_START, ASSEMBLE_END],
    [piece.startRotate, piece.midRotate, piece.midRotate, 0]
  );
  const spin = useTransform(progress, [0, ASSEMBLE_START], [0, piece.spinAmount]);
  const spinFade = useTransform(progress, [ASSEMBLE_START, ASSEMBLE_END], [1, 0]);
  const rotate = useTransform(() => baseRotate.get() + spin.get() * spinFade.get());

  // Opacity: fade out as infinity fades in
  const opacity = useTransform(
    progress,
    isCenter
      ? [0, 0.02, 0.6, ASSEMBLE_START, ASSEMBLE_END]
      : [enter, enter + 0.05, enter + 0.2, ASSEMBLE_START, ASSEMBLE_END],
    isCenter
      ? [0.3, 0.5, 0.85, 0.8, 0]
      : [0, 0.3, 0.75, 0.7, 0]
  );

  return (
    <motion.img
      src={piece.src}
      alt=""
      width={512}
      height={512}
      className={`absolute h-auto ${piece.sizeClass}`}
      style={{
        x,
        y,
        scale,
        rotate,
        opacity,
        filter: "drop-shadow(0 15px 40px rgba(80, 55, 30, 0.12)) drop-shadow(0 5px 15px rgba(60, 45, 30, 0.08))",
      }}
    />
  );
};

export default PuzzleBackground;
