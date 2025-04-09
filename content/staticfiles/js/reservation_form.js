document.addEventListener("DOMContentLoaded", function () {
  const checkbox = document.getElementById("id_need_carseats");
  const carseatOptions = document.getElementById("carseat-options");

  function toggleCarseatVisibility() {
    if (checkbox.checked) {
      carseatOptions.classList.add("show");
      carseatOptions.classList.remove("hide");
    } else {
      carseatOptions.classList.add("hide");
      carseatOptions.classList.remove("show");
    }
  }

  // Call on load in case pre-filled
  toggleCarseatVisibility();

  // Listen to changes
  checkbox.addEventListener("change", toggleCarseatVisibility);
});
