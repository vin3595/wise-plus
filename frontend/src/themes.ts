import velvet from "./theme-previews/velvet.svg";
import ease from "./theme-previews/ease.jpg";
import board from "./theme-previews/board.jpg";
import irani from "./theme-previews/irani.jpg";
import shake from "./theme-previews/shake.jpg";
import steel from "./theme-previews/steel.jpg";
import swiss from "./theme-previews/swiss.jpg";
import type { UiTheme } from "./types";

/** The looks of the staff app (styles in theme.css). Previews are real screenshots of the counter. */
export const THEMES: { id: UiTheme; name: string; about: string; preview: string }[] = [
  { id: "velvet", name: "Ease My Bill night", about: "The Ease My Bill look: charcoal, dusty purple and gold. Easy on the eyes at the counter, day or night.", preview: velvet },
  { id: "ease", name: "Ease classic", about: "The original Ease My Bill look: cool white, soft cards, clean type, in your brand colour.", preview: ease },
  { id: "steel", name: "Steel", about: "The commercial kitchen: brushed steel panels, solid keys, stencil numbers. Light, calm under bright lights.", preview: steel },
  { id: "board", name: "Menu board", about: "The cafe chalkboard: green slate, chalk lettering, prices with dotted leaders. Dark and easy on the eyes at night.", preview: board },
  { id: "irani", name: "Irani cafe", about: "Checkered floor, black outlines, red signage. Loud, old-school, impossible to mis-tap.", preview: irani },
  { id: "swiss", name: "Signboard", about: "White and black with one solid block of your brand colour for the bill. Nothing on screen but what matters.", preview: swiss },
  { id: "shake", name: "Milkshake bar", about: "Your brand colour as pastel, soft rounded keys, parlour lettering. Friendly and bright.", preview: shake },
];
