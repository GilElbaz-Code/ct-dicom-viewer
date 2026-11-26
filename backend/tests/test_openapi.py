from app.main import app

# Every status each route can return, as /api/docs documents it.
EXPECTED = {
    ("/api/directories", "get"): {"200"},
    ("/api/jobs", "post"): {"201", "422"},
    ("/api/batches", "post"): {"201", "422"},
    ("/api/jobs", "get"): {"200", "422"},
    ("/api/series", "get"): {"200", "422"},
    ("/api/series/{series_id}", "get"): {"200", "404", "422"},
    ("/api/series/{series_id}/locate", "get"): {"200", "404", "422"},
    ("/api/series/{series_id}/axial/{index}", "get"): {"200", "404", "422"},
    ("/api/series/{series_id}/sagittal", "get"): {"200", "404", "422"},
    ("/api/health", "get"): {"200"},
}


def test_every_route_documents_the_status_codes_it_returns():
    documented = {
        (path, method): set(operation["responses"])
        for path, operations in app.openapi()["paths"].items()
        for method, operation in operations.items()
    }
    assert documented == EXPECTED
    image = app.openapi()["paths"]["/api/series/{series_id}/sagittal"]["get"]["responses"]["200"]
    assert "image/png" in image["content"]
