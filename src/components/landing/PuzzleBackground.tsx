import { motion, useScroll, useTransform, MotionValue } from "framer-motion";
import { useRef } from "react";
import puzzlePiece1 from "@/assets/puzzle-piece-1.png";
import puzzlePiece2 from "@/assets/puzzle-piece-2.png";
import puzzlePiece3 from "@/assets/puzzle-piece-3.png";
import puzzlePiece4 from "@/assets/puzzle-piece-4.png";
import puzzlePiece6 from "@/assets/puzzle-piece-6.png";

const images = [puzzlePiece1, puzzlePiece2, puzzlePiece3, puzzlePiece4, puzzlePiece6];

interface PieceConfig {
  src: string;
  // Start: off-screen or zoomed, with initial rotation
  startX: number;
  startY: number;
  startScale: number;
  startRotate: number;
  // Spin amount during scroll (added to rotation)
  spinAmount: number;
  // Final position on infinity/fluid curve
  endX: number;
  endY: number;
  endScale: number;
  endRotate: number;
  // Timing: when this piece enters [0-1]
  enterAt: number;
  // Size class
  sizeClass: string;
}

// Arrange final positions along a Zaha Hadid-inspired flowing lemniscate (∞) shape
// Parametric: x = a*cos(t)/(1+sin²(t)), y = a*sin(t)*cos(t)/(1+sin²(t))
const pieces: PieceConfig[] = [
  // CENTER PIECE — hero piece, visible from start
  {
    src: images[0],
    startX: 0, startY: 0, startScale: 3.2, startRotate: 0,
    spinAmount: 360,
    endX: 0, endY: 0, endScale: 0.38, endRotate: 0,
    enterAt: 0,
    sizeClass: "w-[50vw] md:w-[28vw] lg:w-[20vw] max-w-[280px]",
  },
  // RIGHT LOBE — top
  {
    src: images[1],
    startX: 800, startY: -400, startScale: 0.1, startRotate: -90,
    spinAmount: -540,
    endX: 160, endY: -40, endScale: 0.32, endRotate: 15,
    enterAt: 0.08,
    sizeClass: "w-[40vw] md:w-[24vw] lg:w-[17vw] max-w-[240px]",
  },
  // LEFT LOBE — top
  {
    src: images[2],
    startX: -800, startY: -300, startScale: 0.1, startRotate: 120,
    spinAmount: 480,
    endX: -155, endY: -35, endScale: 0.30, endRotate: -20,
    enterAt: 0.1,
    sizeClass: "w-[38vw] md:w-[22vw] lg:w-[16vw] max-w-[220px]",
  },
  // RIGHT LOBE — bottom
  {
    src: images[3],
    startX: 600, startY: 500, startScale: 0.1, startRotate: 200,
    spinAmount: -420,
    endX: 120, endY: 55, endScale: 0.28, endRotate: 40,
    enterAt: 0.12,
    sizeClass: "w-[36vw] md:w-[20vw] lg:w-[15vw] max-w-[210px]",
  },
  // LEFT LOBE — bottom
  {
    src: images[4],
    startX: -700, startY: 400, startScale: 0.1, startRotate: -150,
    spinAmount: 600,
    endX: -130, endY: 50, endScale: 0.26, endRotate: -35,
    enterAt: 0.14,
    sizeClass: "w-[34vw] md:w-[19vw] lg:w-[14vw] max-w-[200px]",
  },
  // FAR RIGHT — extending the flow
  {
    src: images[0],
    startX: 1000, startY: 0, startScale: 0.05, startRotate: 45,
    spinAmount: -720,
    endX: 260, endY: 10, endScale: 0.22, endRotate: 60,
    enterAt: 0.18,
    sizeClass: "w-[30vw] md:w-[17vw] lg:w-[12vw] max-w-[170px]",
  },
  // FAR LEFT — extending the flow
  {
    src: images[1],
    startX: -900, startY: 100, startScale: 0.05, startRotate: -60,
    spinAmount: 540,
    endX: -250, endY: 15, endScale: 0.20, endRotate: -55,
    enterAt: 0.2,
    sizeClass: "w-[28vw] md:w-[16vw] lg:w-[11vw] max-w-[160px]",
  },
  // TOP ACCENT — small floating piece
  {
    src: images[3],
    startX: 200, startY: -600, startScale: 0.05, startRotate: 180,
    spinAmount: -900,
    endX: 60, endY: -90, endScale: 0.18, endRotate: 25,
    enterAt: 0.22,
    sizeClass: "w-[24vw] md:w-[14vw] lg:w-[10vw] max-w-[140px]",
  },
  // BOTTOM ACCENT — small floating piece
  {
    src: images[4],
    startX: -300, startY: 600, startScale: 0.05, startRotate: -200,
    spinAmount: 720,
    endX: -50, endY: 85, endScale: 0.17, endRotate: -30,
    enterAt: 0.24,
    sizeClass: "w-[22vw] md:w-[13vw] lg:w-[9vw] max-w-[130px]",
  },
  // OUTER RIGHT TIP
  {
    src: images[2],
    startX: 1200, startY: -200, startScale: 0.05, startRotate: 90,
    spinAmount: -480,
    endX: 320, endY: -20, endScale: 0.15, endRotate: 75,
    enterAt: 0.26,
    sizeClass: "w-[20vw] md:w-[12vw] lg:w-[8vw] max-w-[120px]",
  },
  // OUTER LEFT TIP
  {
    src: images[0],
    startX: -1100, startY: -150, startScale: 0.05, startRotate: -120,
    spinAmount: 600,
    endX: -310, endY: -25, endScale: 0.14, endRotate: -70,
    enterAt: 0.28,
    sizeClass: "w-[18vw] md:w-[11vw] lg:w-[8vw] max-w-[110px]",
  },
];

const PuzzleBackground = () => {
  const containerRef = useRef<HTMLDivElement>(null);
  const { scrollYProgress } = useScroll({
    target: containerRef,
    offset: ["start start", "end end"],
  });

  return (
    <div ref={containerRef} className="absolute inset-0 z-10 pointer-events-none">
      <div className="sticky top-0 h-screen flex items-center justify-center">
        {pieces.map((piece, i) => (
          <PuzzlePiece key={i} piece={piece} progress={scrollYProgress} />
        ))}
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
  const midPoint = Math.min(enter + 0.3, 0.55);
  const settlePoint = 0.75;

  // Position: fly in → settle into fluid form
  const x = useTransform(
    progress,
    [enter, midPoint, settlePoint],
    [piece.startX, piece.endX * 0.6 + piece.startX * 0.15, piece.endX]
  );
  const y = useTransform(
    progress,
    [enter, midPoint, settlePoint],
    [piece.startY, piece.endY * 0.6 + piece.startY * 0.15, piece.endY]
  );

  // Scale: zoom in center piece → shrink; others: tiny → grow
  const scale = useTransform(
    progress,
    isCenter
      ? [0, 0.2, 0.5, settlePoint]
      : [enter, Math.min(enter + 0.15, 0.4), midPoint, settlePoint],
    isCenter
      ? [piece.startScale, piece.startScale * 0.7, piece.endScale * 1.3, piece.endScale]
      : [piece.startScale, piece.endScale * 0.5, piece.endScale * 0.9, piece.endScale]
  );

  // Rotation: base rotation + continuous spin during scroll
  const baseRotate = useTransform(
    progress,
    [enter, midPoint, settlePoint],
    [piece.startRotate, piece.startRotate * 0.3 + piece.endRotate * 0.7, piece.endRotate]
  );
  const spin = useTransform(progress, [0, 1], [0, piece.spinAmount]);
  const rotate = useTransform(() => baseRotate.get() + spin.get());

  // Opacity: fade in
  const opacity = useTransform(
    progress,
    isCenter
      ? [0, 0.02, 0.6, settlePoint]
      : [enter, enter + 0.05, enter + 0.2, settlePoint],
    isCenter
      ? [0.3, 0.5, 0.85, 1]
      : [0, 0.3, 0.75, 0.95]
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
