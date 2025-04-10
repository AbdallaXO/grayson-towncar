/* 
  Shared Repo PurgeCSS Script
  ---------------------------
  First time:
    npm install -g purgecss
  Then from project root:
    node purgecss-auto.js

  This script purges CSS from: content/static/css/
  and saves cleaned versions to: content/static/css/purged/

  Our HTML files are now using all css under content/static/css/purged/
    - This avoids having to manually update all css files under content/static/css/ 
        after running purgecss script
    - We will keep making edits to css files under content/static/css/ and run the 
        purgecss script before pushing to deployment to ensure all css files are clean
*/


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

// 2. Find all CSS files in content/static/css
const cssDir = path.join(__dirname, "content", "static", "css");
let cssFiles = [];
if (fs.existsSync(cssDir)) {
    const files = fs.readdirSync(cssDir);
    files.forEach((file) => {
        if (file.endsWith(".css")) {
            cssFiles.push(path.join(cssDir, file).replace(/\\/g, "/"));
        }
    });
}

// 3. Find all js files in content/static/js 
const jsDir = path.join(__dirname, "content", "static", "js");
let jsFiles = [];
if (fs.existsSync(jsDir)) {
    const walkDir = (dir) => {
        const files = fs.readdirSync(dir, { withFileTypes: true });
        files.forEach((file) => {
            const fullPath = path.join(dir, file.name);
            if (file.isDirectory()) {
                walkDir(fullPath); // recurse into subdirectories
            } else if (file.name.endsWith(".js")) {
                jsFiles.push(fullPath.replace(/\\/g, "/"));
            }
        });
    };
    walkDir(jsDir);
}

// 4. Run purgecss with collected files
const purgeCommand = `purgecss --content ${[...htmlFiles, ...jsFiles].join(" ")} --css ${cssFiles.join(" ")} --output ${cssDir}/purged`;

console.log("🧼 Running PurgeCSS...\n");
exec(purgeCommand, (err, stdout, stderr) => {
    if (err) {
        console.error("❌ Error:", err);
        return;
    }
    if (stderr) console.warn("⚠️", stderr);
    console.log("✅ PurgeCSS complete.\n", stdout);
});
