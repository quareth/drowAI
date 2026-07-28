/**
 * Colored identity mark for the Pathfinder recon subagent.
 *
 * The source artwork is a project-local transparent raster asset so every
 * Pathfinder surface renders the same stable identity without remote loading.
 */
import type { ImgHTMLAttributes } from "react";

import pathfinderIconUrl from "@/assets/pathfinder-icon.png";

type PathfinderIconProps = Omit<
  ImgHTMLAttributes<HTMLImageElement>,
  "alt" | "src"
>;

export function PathfinderIcon({
  className,
  ...props
}: PathfinderIconProps) {
  return (
    <img
      src={pathfinderIconUrl}
      alt=""
      draggable={false}
      className={className}
      {...props}
    />
  );
}

export default PathfinderIcon;
