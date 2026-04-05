window.tailwind = window.tailwind || {};
window.tailwind.config = {
  theme: {
    extend: {
      colors: {
        bamboo: {
          surface: "#f9faf5",
          desk: "#f2f4ee",
          paper: "#ffffff",
          mist: "#dce5da",
          sage: "#86A789",
          moss: "#466649",
          deep: "#2d342d",
          soft: "#596159",
          highlight: "#c8ecc8",
        },
      },
      fontFamily: {
        ui: ["Manrope", "Segoe UI", "PingFang SC", "Microsoft YaHei", "sans-serif"],
        display: ["Newsreader", "Source Han Serif SC", "Noto Serif SC", "serif"],
        editor: ["Newsreader", "Source Han Serif SC", "Noto Serif SC", "serif"],
      },
      boxShadow: {
        cloud: "0 12px 40px rgba(45, 52, 45, 0.06)",
      },
      borderRadius: {
        bamboo: "1rem",
      },
      backgroundImage: {
        "bamboo-primary":
          "linear-gradient(135deg, rgba(70,102,73,1) 0%, rgba(134,167,137,0.95) 100%)",
      },
    },
  },
};
