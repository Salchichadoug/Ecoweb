document.addEventListener("DOMContentLoaded", () => {
    const userRole = document.body.dataset.userRole || "invitado";
    const userName = document.body.dataset.userName || "";
    const canManageCleanings = userRole === "trabajador" || userRole === "administrador";

    const SENSOR_POLL_INTERVAL_MS = 15000;

    const DEMO_DATA = {
        deviceStatus: "En funcionamiento",
        deviceConnection: "Conectado · Estado simulado"
    };

    const CAMERA_CONFIG = {
        ip: "",
        streamUrl: ""
    };

    const elements = {
        deviceStatus: document.querySelector("#device-status"),
        deviceConnection: document.querySelector("#device-connection"),

        visualStatus: document.querySelector("#visual-status"),
        visualStatusLabel: document.querySelector("#visual-status-label"),
        reviewIndicator: document.querySelector(".review-indicator"),

        lastCleaningDate: document.querySelector("#last-cleaning-date"),
        lastCleaningTime: document.querySelector("#last-cleaning-time"),
        userLastCleaningSummary: document.querySelector("#user-last-cleaning-summary"),

        historyRows: document.querySelector("#history-rows"),
        historyCount: document.querySelector("#history-count"),

        cleaningForm: document.querySelector("#cleaning-form"),
        cleaningPerson: document.querySelector("#cleaning-person"),
        cleaningDate: document.querySelector("#cleaning-date"),
        cleaningAction: document.querySelector("#cleaning-action"),
        cleaningObservations: document.querySelector("#cleaning-observations"),
        cleaningFeedback: document.querySelector("#cleaning-feedback"),

        cameraForm: document.querySelector("#camera-form"),
        cameraIp: document.querySelector("#camera-ip"),
        cameraUrl: document.querySelector("#camera-url"),
        cameraFeedback: document.querySelector("#camera-feedback"),

        refreshButton: document.querySelector("#refresh-demo"),
        updatedAt: document.querySelector("#updated-at"),
        signalTime: document.querySelector("#signal-time")
    };

    const VISUAL_STATUS_LABELS = {
        pending: "Pendiente de revisión",
        available: "Todavía hay espacio",
        filling: "Se acerca al límite",
        cleaning: "Requiere limpieza"
    };

    function formatAction(action) {
        const actions = {
            vaciado: "Se vació el recolector",
            filtro: "Se cambió el filtro",
            ambos: "Vaciado y cambio de filtro"
        };

        return actions[action] || action || "No especificada";
    }

    function splitDateTime(value) {
        if (!value) {
            return {
                datePart: "Sin registros",
                timePart: "--:--",
                fullLabel: "Sin registros"
            };
        }

        const date = new Date(value);

        if (Number.isNaN(date.getTime())) {
            return {
                datePart: value,
                timePart: "--:--",
                fullLabel: value
            };
        }

        const datePart = date.toLocaleDateString("es-AR", {
            day: "2-digit",
            month: "short",
            year: "numeric"
        });

        const timePart = date.toLocaleTimeString("es-AR", {
            hour: "2-digit",
            minute: "2-digit"
        });

        return {
            datePart,
            timePart,
            fullLabel: `${datePart} · ${timePart}`
        };
    }

    function createCell(value) {
        const cell = document.createElement("td");
        cell.textContent = value || "—";
        return cell;
    }

    function setFeedback(message, isError = false) {
        if (!elements.cleaningFeedback) return;

        elements.cleaningFeedback.textContent = message;
        elements.cleaningFeedback.style.color = isError ? "#b42318" : "";
    }

    function setCurrentDateTime() {
        if (!elements.cleaningDate) return;

        const now = new Date();
        const localDateTime = new Date(
            now.getTime() - now.getTimezoneOffset() * 60000
        );

        elements.cleaningDate.value = localDateTime.toISOString().slice(0, 16);
    }

    function prefillResponsible() {
        if (elements.cleaningPerson && userName && !elements.cleaningPerson.value) {
            elements.cleaningPerson.value = userName;
        }
    }

    function renderDeviceStatus(customConnectionText = null) {
        if (elements.deviceStatus) {
            elements.deviceStatus.textContent = DEMO_DATA.deviceStatus;
        }

        if (elements.deviceConnection) {
            elements.deviceConnection.textContent =
                customConnectionText || DEMO_DATA.deviceConnection;
        }
    }

    function applyVisualStatus(statusKey) {
        const validStatus = VISUAL_STATUS_LABELS[statusKey] ? statusKey : "pending";
        const label = VISUAL_STATUS_LABELS[validStatus];

        if (elements.visualStatus && elements.visualStatus.value !== validStatus) {
            elements.visualStatus.value = validStatus;
        }

        if (elements.visualStatusLabel) {
            elements.visualStatusLabel.textContent = label;
        }

        if (elements.reviewIndicator) {
            elements.reviewIndicator.dataset.status = validStatus;
        }
    }

    function updateVisualStatus() {
        if (!elements.visualStatus) return;
        applyVisualStatus(elements.visualStatus.value);
    }

    if (elements.visualStatus) {
        elements.visualStatus.addEventListener("change", updateVisualStatus);
    }

    async function loadSensorStatus() {
        try {
            const response = await fetch("/api/estado-cesto");
            if (!response.ok) {
                throw new Error("No se pudo consultar el estado del cesto.");
            }

            const data = await response.json();

            if (data.estado_visual) {
                applyVisualStatus(data.estado_visual);
            }

            if (data.ultima_alerta) {
                const parsedAlert = splitDateTime(data.ultima_alerta);
                renderDeviceStatus(`Última alerta de llenado: ${parsedAlert.fullLabel}`);
                if (elements.signalTime && parsedAlert.timePart !== "--:--") {
                    elements.signalTime.textContent = parsedAlert.timePart;
                }
            } else {
                renderDeviceStatus();
            }

            return true;
        } catch (error) {
            console.error("Error al consultar el estado del cesto:", error);
            return false;
        }
    }

    function updateLastCleaningCard(datePart, timePart) {
        if (elements.lastCleaningDate) {
            elements.lastCleaningDate.textContent = datePart;
        }

        if (elements.lastCleaningTime) {
            elements.lastCleaningTime.textContent =
                timePart && timePart !== "--:--" ? `${timePart} h` : "--:-- h";
        }
    }

    function renderHistory(cleanings) {
        if (!elements.historyRows) return;

        elements.historyRows.replaceChildren();

        if (elements.historyCount) {
            elements.historyCount.textContent =
                `${cleanings.length} registro${cleanings.length === 1 ? "" : "s"}`;
        }

        if (cleanings.length === 0) {
            const row = document.createElement("tr");
            const cell = document.createElement("td");

            cell.colSpan = 4;
            cell.textContent = "Todavía no hay limpiezas registradas.";

            row.appendChild(cell);
            elements.historyRows.appendChild(row);
            updateLastCleaningCard("Sin registros", "--:--");
            return;
        }

        cleanings.forEach((cleaning) => {
            const row = document.createElement("tr");

            row.appendChild(createCell(cleaning.fullLabel));
            row.appendChild(createCell(cleaning.person));
            row.appendChild(createCell(formatAction(cleaning.action)));
            row.appendChild(createCell(cleaning.observations));

            elements.historyRows.appendChild(row);
        });

        const latest = cleanings[0];
        updateLastCleaningCard(latest.datePart, latest.timePart);
    }

    async function loadLastCleaningOnly() {
        try {
            const response = await fetch("/api/ultima-limpieza");
            if (!response.ok) {
                throw new Error("No se pudo consultar la última limpieza.");
            }

            const data = await response.json();
            const parsed = splitDateTime(data.fecha);

            updateLastCleaningCard(parsed.datePart, parsed.timePart);

            if (elements.historyCount) {
                elements.historyCount.textContent = data.fecha ? "Última fecha" : "Sin registros";
            }

            if (elements.userLastCleaningSummary) {
                elements.userLastCleaningSummary.textContent = data.fecha
                    ? `Última limpieza: ${parsed.fullLabel}.`
                    : "Todavía no hay limpiezas registradas.";
            }

            return true;
        } catch (error) {
            console.error("Error al cargar la última limpieza:", error);
            if (elements.userLastCleaningSummary) {
                elements.userLastCleaningSummary.textContent =
                    "No se pudo consultar cuándo fue la última limpieza.";
            }
            return false;
        }
    }

    async function loadCleanings() {
        if (!canManageCleanings) {
            return loadLastCleaningOnly();
        }

        try {
            const response = await fetch("/api/limpiezas");

            if (!response.ok) {
                throw new Error("No se pudo consultar la base de datos.");
            }

            const records = await response.json();

            const cleanings = records.map((record) => {
                const parsed = splitDateTime(record.fecha);
                return {
                    id: record.id,
                    datePart: parsed.datePart,
                    timePart: parsed.timePart,
                    fullLabel: parsed.fullLabel,
                    person: record.responsable,
                    action: record.accion,
                    observations: record.observaciones
                };
            });

            renderHistory(cleanings);
            return true;
        } catch (error) {
            console.error("Error al cargar las limpiezas:", error);

            setFeedback(
                "No se pudo cargar el historial. Verificá que Flask esté funcionando.",
                true
            );

            return false;
        }
    }

    if (elements.cleaningForm && canManageCleanings) {
        elements.cleaningForm.addEventListener("submit", async (event) => {
            event.preventDefault();

            const data = {
                responsable: elements.cleaningPerson.value.trim(),
                fecha: elements.cleaningDate.value,
                accion: elements.cleaningAction.value,
                observaciones: elements.cleaningObservations.value.trim()
            };

            if (!data.responsable || !data.fecha || !data.accion) {
                setFeedback(
                    "Completá el responsable, la fecha, la hora y la tarea.",
                    true
                );
                return;
            }

            const submitButton = elements.cleaningForm.querySelector(
                'button[type="submit"]'
            );

            if (submitButton) {
                submitButton.disabled = true;
                submitButton.textContent = "Guardando...";
            }

            try {
                const response = await fetch("/api/limpiezas", {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json"
                    },
                    body: JSON.stringify(data)
                });

                const result = await response.json();

                if (!response.ok) {
                    throw new Error(
                        result.error || "No se pudo guardar la limpieza."
                    );
                }

                setFeedback(
                    "¡Limpieza guardada correctamente en la base de datos!"
                );

                elements.cleaningAction.value = "";
                elements.cleaningObservations.value = "";
                setCurrentDateTime();
                prefillResponsible();

                applyVisualStatus("available");

                await Promise.all([loadCleanings(), loadSensorStatus()]);
            } catch (error) {
                console.error("Error al registrar la limpieza:", error);

                setFeedback(
                    error.message || "Error de conexión con el servidor.",
                    true
                );
            } finally {
                if (submitButton) {
                    submitButton.disabled = false;
                    submitButton.textContent = "+ Registrar limpieza";
                }
            }
        });
    }

    if (elements.cameraForm) {
        elements.cameraForm.addEventListener("submit", (event) => {
            event.preventDefault();

            CAMERA_CONFIG.ip = elements.cameraIp
                ? elements.cameraIp.value.trim()
                : "";

            CAMERA_CONFIG.streamUrl = elements.cameraUrl
                ? elements.cameraUrl.value.trim()
                : "";

            if (!CAMERA_CONFIG.ip && !CAMERA_CONFIG.streamUrl) {
                if (elements.cameraFeedback) {
                    elements.cameraFeedback.textContent =
                        "Completá la IP o la URL cuando tengas los datos. No se realiza ninguna conexión.";
                }
                return;
            }

            if (elements.cameraFeedback) {
                elements.cameraFeedback.textContent =
                    "Datos guardados solo durante esta sesión. Todavía no se conecta la cámara real.";
            }
        });
    }

    if (elements.refreshButton) {
        elements.refreshButton.addEventListener("click", async () => {
            const time = new Date().toLocaleTimeString("es-AR", {
                hour: "2-digit",
                minute: "2-digit"
            });

            if (elements.updatedAt) {
                elements.updatedAt.textContent = "Actualizando...";
            }

            if (elements.signalTime) {
                elements.signalTime.textContent = time;
            }

            await Promise.all([loadCleanings(), loadSensorStatus()]);

            if (elements.updatedAt) {
                elements.updatedAt.textContent = `Vista actualizada a las ${time}`;
            }
        });
    }

    renderDeviceStatus();
    updateVisualStatus();
    setCurrentDateTime();
    prefillResponsible();
    loadCleanings();
    loadSensorStatus();

    setInterval(loadSensorStatus, SENSOR_POLL_INTERVAL_MS);

    const welcomeScreen = document.querySelector("#welcome-screen");
    const enterBtn = document.querySelector("#enter-btn");

    function ocultarPortada(e) {
        if (e) e.stopPropagation();
        if (welcomeScreen) {
            welcomeScreen.classList.add("is-hidden");
        }
    }

    if (enterBtn) {
        enterBtn.addEventListener("click", ocultarPortada);
    }

    if (welcomeScreen) {
        welcomeScreen.addEventListener("click", ocultarPortada);
    }
});