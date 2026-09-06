import { createApp } from "vue";
import { createPinia } from "pinia";
import App from "./App.vue";
import "./styles/base.css";
import "./styles/shell.css";
import "./styles/views.css";
import "./styles/model.css";
import "./styles/image.css";
import "./styles/theme.css";

document.documentElement.dataset.theme = "midnight-workshop";
document.documentElement.dataset.density = "compact";

const app = createApp(App);
const pinia = createPinia();
app.use(pinia);
app.mount("#app");
