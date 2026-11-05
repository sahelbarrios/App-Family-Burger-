var FOLDER_ID = "1FhnbLCTj1cjeJvqnmB0JuMiHRFxHVHHu";
var TOKEN = "CAMBIA-ESTO-POR-UNA-CLAVE-LARGA";

function doPost(e) {
  try {
    var d = JSON.parse(e.postData.contents);
    if (d.token !== TOKEN) {
      return salida({ ok: false, error: "Token incorrecto" });
    }
    var carpeta = DriveApp.getFolderById(FOLDER_ID);

    if (d.accion === "subir") {
      var blob = Utilities.newBlob(Utilities.base64Decode(d.data), d.mime, d.nombre);
      var archivo = carpeta.createFile(blob);
      return salida({ ok: true, id: archivo.getId() });
    }

    if (d.accion === "leer") {
      var f = DriveApp.getFileById(d.id);
      var padres = f.getParents();
      var dentro = false;
      while (padres.hasNext()) {
        if (padres.next().getId() === FOLDER_ID) {
          dentro = true;
        }
      }
      if (!dentro) {
        return salida({ ok: false, error: "Archivo fuera de la carpeta" });
      }
      return salida({ ok: true, mime: f.getMimeType(), data: Utilities.base64Encode(f.getBlob().getBytes()) });
    }

    return salida({ ok: false, error: "Accion desconocida" });
  } catch (err) {
    return salida({ ok: false, error: String(err) });
  }
}

function salida(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
