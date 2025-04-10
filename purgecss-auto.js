/* How-To: 
    First run: npm install -g purgecss (once for each machine)
    From project root run: node purgecss-auto.js
    Output will be located at static/css/purged/ with purged files */


const fs = require("fs");
const path = require("path");
const { exec } = require("child_process");

const appDirs = [
    "blog", "content", "payment", "rates",
    "reservations", "services", "users"
];

// 1. Find all HTML templates inside app directories
let htmlFiles = [];
appDirs.forEach((app) => {
    const templateDir = path.join(__dirname, app, "templates");
    if (fs.existsSync(templateDir)) {
        const files = fs.readdirSync(templateDir, { withFileTypes: true });
        files.forEach((file) => {
            const fullPath = path.join(templateDir, file.name);
            if (file.isDirectory()) {
                htmlFiles.push(`${templateDir}/**/*.html`);
            } else if (file.name.endsWith(".html")) {
                htmlFiles.push(fullPath.replace(/\\/g, "/"));
            }
        });
    }
});

// 2. Find all CSS files in static/css
const cssDir = path.join(__dirname, "static", "css");
let cssFiles = [];
if (fs.existsSync(cssDir)) {
    const files = fs.readdirSync(cssDir);
    files.forEach((file) => {
        if (file.endsWith(".css")) {
            cssFiles.push(path.join(cssDir, file).replace(/\\/g, "/"));
        }
    });
}

// 3. Run purgecss with collected files
const purgeCommand = `purgecss --content ${htmlFiles.join(" ")} --css ${cssFiles.join(" ")} --output ${cssDir}/purged`;

console.log("🧼 Running PurgeCSS...\n");
exec(purgeCommand, (err, stdout, stderr) => {
    if (err) {
        console.error("❌ Error:", err);
        return;
    }
    if (stderr) console.warn("⚠️", stderr);
    console.log("✅ PurgeCSS complete.\n", stdout);
});