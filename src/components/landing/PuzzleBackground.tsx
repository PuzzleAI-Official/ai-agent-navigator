import { motion, useScroll, useTransform, MotionValue } from "framer-motion";
import { useRef } from "react";
import infinityPiece1 from "@/assets/infinity-piece-1.png";
import infinityPiece2 from "@/assets/infinity-piece-2.png";
import infinityPiece3 from "@/assets/infinity-piece-3.png";
import infinityPiece4 from "@/assets/infinity-piece-4.png";
import infinityPiece5 from "@/assets/infinity-piece-5.png";
import infinityPiece6 from "@/assets/infinity-piece-6.png";

const images = [infinityPiece1, infinityPiece2, infinityPiece3, infinityPiece4, infinityPiece5, infinityPiece6];

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
  endX: number;
  endY: number;
  endScale: number;
  endRotate: number;
  enterAt: number;
  sizeClass: string;
  endOpacity: number;
}

// Pieces fly in → scatter → converge into layered infinity symbol
const pieces: PieceConfig[] = [
  // Core center piece — largest, anchor
  {
    src: images[0],
    startX: 0, startY: 0, startScale: 3.0, startRotate: 0,
    spinAmount: 180,
    midX: 0, midY: 0, midScale: 0.5, midRotate: 0,
    endX: 0, endY: 0, endScale: 0.55, endRotate: 0,
    enterAt: 0, endOpacity: 1,
    sizeClass: "w-[60vw] md:w-[35vw] lg:w-[28vw] max-w-[400px]",
  },
  // Layer 2 — slight offset, different hue
  {
    src: images[1],
    startX: 700, startY: -350, startScale: 0.1, startRotate: -120,
    spinAmount: -360,
    midX: 180, midY: -60, midScale: 0.35, midRotate: 20,
    endX: 0, endY: 0, endScale: 0.55, endRotate: 0,
    enterAt: 0.06, endOpacity: 0.7,
    sizeClass: "w-[60vw] md:w-[35vw] lg:w-[28vw] max-w-[400px]",
  },
  // Layer 3 — from left
  {
    src: images[2],
    startX: -800, startY: -200, startScale: 0.1, startRotate: 150,
    spinAmount: 420,
    midX: -200, midY: -40, midScale: 0.32, midRotate: -25,
    endX: 0, endY: 0, endScale: 0.55, endRotate: 0,
    enterAt: 0.1, endOpacity: 0.6,
    sizeClass: "w-[60vw] md:w-[35vw] lg:w-[28vw] max-w-[400px]",
  },
  // Layer 4 — from bottom right
  {
    src: images[3],
    startX: 600, startY: 500, startScale: 0.1, startRotate: 200,
    spinAmount: -540,
    midX: 150, midY: 80, midScale: 0.28, midRotate: 40,
    endX: 0, endY: 0, endScale: 0.55, endRotate: 0,
    enterAt: 0.14, endOpacity: 0.5,
    sizeClass: "w-[60vw] md:w-[35vw] lg:w-[28vw] max-w-[400px]",
  },
  // Layer 5 — from bottom left
  {
    src: images[4],
    startX: -700, startY: 400, startScale: 0.1, startRotate: -180,
    spinAmount: 600,
    midX: -160, midY: 70, midScale: 0.25, midRotate: -35,
    endX: 0, endY: 0, endScale: 0.55, endRotate: 0,
    enterAt: 0.18, endOpacity: 0.45,
    sizeClass: "w-[60vw] md:w-[35vw] lg:w-[28vw] max-w-[400px]",
  },
  // Layer 6 — accent glow from far
  {
    src: images[5],
    startX: 900, startY: -100, startScale: 0.05, startRotate: 90,
    spinAmount: -720,
    midX: 250, midY: 20, midScale: 0.2, midRotate: 60,
    endX: 0, endY: 0, endScale: 0.58, endRotate: 0,
    enterAt: 0.22, endOpacity: 0.35,
    sizeClass: "w-[60vw] md:w-[35vw] lg:w-[28vw] max-w-[400px]",
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
  const scatterPoint = Math.min(enter + 0.3, 0.55);
  const assembleStart = 0.65;
  const assembleEnd = 0.85;

  const x = useTransform(
    progress,
    [enter, scatterPoint, assembleStart, assembleEnd],
    [piece.startX, piece.midX, piece.midX, piece.endX]
  );
  const y = useTransform(
    progress,
    [enter, scatterPoint, assembleStart, assembleEnd],
    [piece.startY, piece.midY, piece.midY, piece.endY]
  );

  const scale = useTransform(
    progress,
    isCenter
      ? [0, 0.2, 0.5, assembleStart, assembleEnd]
      : [enter, Math.min(enter + 0.15, 0.4), scatterPoint, assembleStart, assembleEnd],
    isCenter
      ? [piece.startScale, piece.startScale * 0.7, piece.midScale * 1.3, piece.midScale, piece.endScale]
      : [piece.startScale, piece.midScale * 0.5, piece.midScale, piece.midScale, piece.endScale]
  );

  const baseRotate = useTransform(
    progress,
    [enter, scatterPoint, assembleStart, assembleEnd],
    [piece.startRotate, piece.midRotate, piece.midRotate, piece.endRotate]
  );
  const spin = useTransform(progress, [0, assembleStart], [0, piece.spinAmount]);
  const spinFade = useTransform(progress, [assembleStart, assembleEnd], [1, 0]);
  const rotate = useTransform(() => baseRotate.get() + spin.get() * spinFade.get());

  const opacity = useTransform(
    progress,
    isCenter
      ? [0, 0.02, 0.6, assembleEnd]
      : [enter, enter + 0.05, enter + 0.2, assembleEnd],
    isCenter
      ? [0.3, 0.5, 0.85, piece.endOpacity]
      : [0, 0.3, 0.75, piece.endOpacity]
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
