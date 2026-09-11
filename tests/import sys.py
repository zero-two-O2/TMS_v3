import sys
import qdarkstyle
from PySide6.QtWidgets import QApplication, QMainWindow

app = QApplication(sys.argv)

app.setStyleSheet(qdarkstyle.load_stylesheet())

window = QMainWindow()
window.setWindowTitle("Thermal Monitor")
window.resize(1200, 800)
window.show()

sys.exit(app.exec())