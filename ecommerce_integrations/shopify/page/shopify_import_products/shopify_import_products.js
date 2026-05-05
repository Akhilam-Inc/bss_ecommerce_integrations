frappe.provide('shopify');

frappe.pages['shopify-import-products'].on_page_load = function (wrapper) {
	let page = frappe.ui.make_app_page({
		parent: wrapper,
		title: 'Import Shopify Products',
		single_column: true
	});

	new shopify.ProductImporter(wrapper);
};

shopify.ProductImporter = class {

	constructor(wrapper) {
		this.wrapper = $(wrapper).find('.layout-main-section');
		this.page = wrapper.page;
		this.syncRunning = false;
		this.currentPage = 1;
		this.nextUrl = null;
		this.prevUrl = null;
		this.init();
	}

	init() {
		frappe.run_serially([
			() => this.addMarkup(),
			() => this.fetchProductCount(),
			() => this.addTable(),
			() => this.checkSyncStatus(),
			() => this.listen(),
		]);
	}

	async checkSyncStatus() {
		const jobs = await frappe.db.get_list("RQ Job", {
			filters: { status: ["in", ["queued", "started"]] }
		});
		this.syncRunning = jobs.find(j => j.job_name === SYNC_JOB_NAME) !== undefined;

		if (this.syncRunning) {
			this.toggleSyncAllButton();
			this.logSync();
		}
	}

	addMarkup() {
		const _markup = $(`
			<div class="row">
				<div class="col-lg-8 d-flex align-items-stretch">
					<div class="card border-0 shadow-sm p-3 mb-3 w-100 rounded-sm" style="background-color: var(--card-bg)">
						<h5 class="border-bottom pb-2">Products in Shopify</h5>

						<div class="shopify-filter-bar mb-3 d-flex flex-wrap align-items-center" style="gap: 8px;">
							<input type="text" class="form-control form-control-sm" id="filter-title"
								placeholder="Search by title..." style="max-width: 220px;">
							<select class="form-control form-control-sm" id="filter-synced" style="max-width: 150px;">
								<option value="">All Products</option>
								<option value="synced">Synced</option>
								<option value="not_synced">Not Synced</option>
							</select>
							<button type="button" class="btn btn-sm btn-primary" id="btn-apply-filters">Search</button>
							<button type="button" class="btn btn-sm btn-default" id="btn-clear-filters">Clear</button>
						</div>

						<div id="not-synced-notice" class="alert alert-warning small py-2 px-3 mb-2" style="display: none;">
							Showing unsynced products from this page only — use title search to find specific items.
						</div>

						<div id="shopify-product-list">
							<div class="text-center py-4 text-muted">Loading...</div>
						</div>

						<div class="shopify-datatable-footer mt-2 pt-3 pb-1 border-top d-flex justify-content-between align-items-center" style="display: none !important;">
							<span class="text-muted small" id="pagination-info">Page 1</span>
							<div class="btn-group">
								<button type="button" class="btn btn-sm btn-default btn-paginate btn-prev">← Prev</button>
								<button type="button" class="btn btn-sm btn-default btn-paginate btn-next">Next →</button>
							</div>
						</div>
					</div>
				</div>
				<div class="col-lg-4 d-flex align-items-stretch">
					<div class="w-100">
						<div class="card border-0 shadow-sm p-3 mb-3 rounded-sm" style="background-color: var(--card-bg)">
							<h5 class="border-bottom pb-2">Synchronization Details</h5>
							<div id="shopify-sync-info">
								<div class="py-3 border-bottom">
									<button type="button" id="btn-sync-all" class="btn btn-xl btn-primary w-100 font-weight-bold py-3">Sync all Products</button>
								</div>
								<div class="product-count py-3 d-flex justify-content-stretch">
									<div class="text-center p-3 mx-2 rounded w-100" style="background-color: var(--bg-color)">
										<h2 id="count-products-shopify">-</h2>
										<p class="text-muted m-0">in Shopify</p>
									</div>
									<div class="text-center p-3 mx-2 rounded w-100" style="background-color: var(--bg-color)">
										<h2 id="count-products-erpnext">-</h2>
										<p class="text-muted m-0">in ERPNext</p>
									</div>
									<div class="text-center p-3 mx-2 rounded w-100" style="background-color: var(--bg-color)">
										<h2 id="count-products-synced">-</h2>
										<p class="text-muted m-0">Synced</p>
									</div>
								</div>
							</div>
						</div>

						<div class="card border-0 shadow-sm p-3 mb-3 rounded-sm" id="sync-log-card" style="background-color: var(--card-bg); display: none;">
							<h5 class="border-bottom pb-2">Sync Log</h5>
							<div class="control-value like-disabled-input for-description overflow-auto" id="shopify-sync-log" style="max-height: 500px;"></div>
						</div>
					</div>
				</div>
			</div>
		`);

		this.wrapper.append(_markup);
	}

	async fetchProductCount() {
		try {
			const { message: { erpnextCount, shopifyCount, syncedCount } } = await frappe.call({
				method: 'ecommerce_integrations.shopify.page.shopify_import_products.shopify_import_products.get_product_count'
			});

			this.wrapper.find('#count-products-shopify').text(shopifyCount);
			this.wrapper.find('#count-products-erpnext').text(erpnextCount);
			this.wrapper.find('#count-products-synced').text(syncedCount);
		} catch (error) {
			frappe.throw(__('Error fetching product count.'));
		}
	}

	async addTable() {
		const listElement = this.wrapper.find('#shopify-product-list')[0];
		this.shopifyProductTable = new frappe.DataTable(listElement, {
			columns: [
				{ name: 'ID',     align: 'left',   editable: false, focusable: false },
				{ name: 'Name',                    editable: false, focusable: false },
				{ name: 'SKUs',                    editable: false, focusable: false },
				{ name: 'Status', align: 'center', editable: false, focusable: false },
				{ name: 'Action', align: 'center', editable: false, focusable: false },
			],
			data: await this.fetchShopifyProducts(),
			layout: 'fixed',
		});

		// footer uses inline display:none from markup; override to show it
		this.wrapper.find('.shopify-datatable-footer').attr('style', '').show();
	}

	// ── Filters ────────────────────────────────────────────────────────────────

	getFilters() {
		return {
			title:         this.wrapper.find('#filter-title').val().trim() || null,
			status:        'active',
			synced_filter: this.wrapper.find('#filter-synced').val() || null,
		};
	}

	async applyFilters() {
		this.currentPage = 1;
		this.nextUrl = null;
		this.prevUrl = null;

		this.shopifyProductTable.showToastMessage('Loading...');
		const rows = await this.fetchShopifyProducts(null, this.getFilters());
		this.shopifyProductTable.refresh(rows);
		this.shopifyProductTable.clearToastMessage();
	}

	async clearFilters() {
		this.wrapper.find('#filter-title').val('');
		this.wrapper.find('#filter-synced').val('');
		await this.applyFilters();
	}

	// ── Data fetching ──────────────────────────────────────────────────────────

	async fetchShopifyProducts(from_ = null, filters = null) {
		if (!filters) filters = this.getFilters();

		try {
			const { message: { products, nextUrl, prevUrl } } = await frappe.call({
				method: 'ecommerce_integrations.shopify.page.shopify_import_products.shopify_import_products.get_shopify_products',
				args: { from_, ...filters },
			});

			this.nextUrl = nextUrl;
			this.prevUrl = prevUrl;
			this.updatePagination();

			// Show notice only when filtering unsynced (client-side, per-page)
			const noticeEl = this.wrapper.find('#not-synced-notice');
			filters.synced_filter === 'not_synced' ? noticeEl.show() : noticeEl.hide();

			return products.map(product => ({
				'ID':     product.id,
				'Name':   product.title,
				'SKUs':   (product.variants || []).map(v => v.sku).join(', '),
				'Status': this.getProductSyncStatus(product.synced),
				'Action': product.synced
					? `<button type="button" class="btn btn-default btn-xs btn-resync mx-2" data-product="${product.id}">Re-sync</button>`
					: `<button type="button" class="btn btn-default btn-xs btn-sync mx-2"   data-product="${product.id}">Sync</button>`,
			}));
		} catch (error) {
			frappe.throw(__('Error fetching products.'));
		}
	}

	// ── Pagination ─────────────────────────────────────────────────────────────

	updatePagination() {
		this.wrapper.find('.btn-prev').prop('disabled', !this.prevUrl);
		this.wrapper.find('.btn-next').prop('disabled', !this.nextUrl);
		this.wrapper.find('#pagination-info').text(`Page ${this.currentPage}`);
	}

	async switchPage({ currentTarget }) {
		const isNext = $(currentTarget).hasClass('btn-next');
		const targetUrl = isNext ? this.nextUrl : this.prevUrl;
		if (!targetUrl) return;

		this.wrapper.find('.btn-paginate').prop('disabled', true);
		this.shopifyProductTable.showToastMessage('Loading...');

		this.currentPage = Math.max(1, this.currentPage + (isNext ? 1 : -1));

		const rows = await this.fetchShopifyProducts(targetUrl, this.getFilters());
		this.shopifyProductTable.refresh(rows);

		this.wrapper.find('.btn-paginate').prop('disabled', false);
		this.shopifyProductTable.clearToastMessage();
	}

	// ── Sync actions ───────────────────────────────────────────────────────────

	getProductSyncStatus(status) {
		return status
			? `<span class="indicator-pill green">Synced</span>`
			: `<span class="indicator-pill orange">Not Synced</span>`;
	}

	async syncProduct(product) {
		const { message: status } = await frappe.call({
			method: 'ecommerce_integrations.shopify.page.shopify_import_products.shopify_import_products.sync_product',
			args: { product },
		});
		if (status) this.fetchProductCount();
		return status;
	}

	async resyncProduct(product) {
		const { message: status } = await frappe.call({
			method: 'ecommerce_integrations.shopify.page.shopify_import_products.shopify_import_products.resync_product',
			args: { product },
		});
		if (status) this.fetchProductCount();
		return status;
	}

	syncAll() {
		this.checkSyncStatus();
		this.toggleSyncAllButton();

		if (this.syncRunning) {
			frappe.msgprint(__('Sync already in progress'));
		} else {
			frappe.call({
				method: 'ecommerce_integrations.shopify.page.shopify_import_products.shopify_import_products.import_all_products'
			});
		}

		this.logSync();
	}

	// ── Event listeners ────────────────────────────────────────────────────────

	listen() {
		// Row-level Sync button
		this.wrapper.on('click', '.btn-sync', e => {
			const btn = $(e.currentTarget).prop('disabled', true).text('Syncing...');
			const product = btn.attr('data-product');

			this.syncProduct(product).then(status => {
				if (!status) {
					btn.prop('disabled', false).text('Sync');
					frappe.throw(__('Error syncing product'));
					return;
				}
				btn.closest('.dt-row').find('.indicator-pill').replaceWith(this.getProductSyncStatus(true));
				btn.replaceWith(`<button type="button" class="btn btn-default btn-xs btn-resync mx-2" data-product="${product}">Re-sync</button>`);
			});
		});

		// Row-level Re-sync button
		this.wrapper.on('click', '.btn-resync', e => {
			const btn = $(e.currentTarget).prop('disabled', true).text('Syncing...');
			const product = btn.attr('data-product');

			this.resyncProduct(product)
				.then(status => {
					btn.prop('disabled', false).text('Re-sync');
					if (!status) { frappe.throw(__('Error syncing product')); return; }
					btn.closest('.dt-row').find('.indicator-pill').replaceWith(this.getProductSyncStatus(true));
				})
				.catch(() => {
					btn.prop('disabled', false).text('Re-sync');
					frappe.throw(__('Error syncing product'));
				});
		});

		// Pagination
		this.wrapper.on('click', '.btn-prev, .btn-next', e => this.switchPage(e));

		// Filters
		this.wrapper.on('click', '#btn-apply-filters', () => this.applyFilters());
		this.wrapper.on('click', '#btn-clear-filters', () => this.clearFilters());
		this.wrapper.on('keydown', '#filter-title', e => { if (e.key === 'Enter') this.applyFilters(); });

		// Sync all
		this.wrapper.on('click', '#btn-sync-all', () => this.syncAll());
	}

	// ── Sync-all log ───────────────────────────────────────────────────────────

	logSync() {
		const _log = this.wrapper.find('#shopify-sync-log');
		this.wrapper.find('#sync-log-card').show();
		_log.empty();

		const _syncedCounter = this.wrapper.find('#count-products-synced');
		const _erpnextCounter = this.wrapper.find('#count-products-erpnext');

		frappe.realtime.on('shopify.key.sync.all.products', ({ message, synced, done }) => {
			_log.append(`<pre class="mb-0">${message}</pre>`);
			_log.scrollTop(_log[0].scrollHeight);

			if (synced) this.updateSyncedCount(_syncedCounter, _erpnextCounter);

			if (done) {
				frappe.realtime.off('shopify.key.sync.all.products');
				this.toggleSyncAllButton(false);
				this.fetchProductCount();
				this.syncRunning = false;
			}
		});
	}

	toggleSyncAllButton(disable = true) {
		const btn = this.wrapper.find('#btn-sync-all');
		btn.prop('disabled', disable)
			.toggleClass('btn-success', disable)
			.toggleClass('btn-primary', !disable)
			.text(disable ? 'Syncing...' : 'Sync Products');
	}

	updateSyncedCount(_syncedCounter, _erpnextCounter) {
		_syncedCounter.text(parseFloat(_syncedCounter.text()) + 1);
		_erpnextCounter.text(parseFloat(_erpnextCounter.text()) + 1);
	}
};

const SYNC_JOB_NAME = 'shopify.job.sync.all.products';
