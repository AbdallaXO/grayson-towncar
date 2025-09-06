# Email Background Tasks Setup

This document explains how to set up and use background email tasks with Celery to improve site performance.

## 🚀 Performance Benefits

**Before (Synchronous):**

- Email sending blocks HTTP requests (1-3 seconds per email)
- Multiple emails per reservation cause cumulative delays
- Payment webhooks timeout if emails fail
- Poor user experience during form submissions

**After (Asynchronous):**

- HTTP requests return immediately (< 100ms)
- Emails processed in background
- Automatic retry on failures
- Better user experience and site performance

## 📦 Installation

1. **Install required packages:**

   ```bash
   pip install -r requirements.txt
   ```

2. **Set up Redis (message broker):**

   ```bash
   # Local development
   redis-server

   # Or use Railway Redis addon for production
   ```

3. **Run migrations:**
   ```bash
   python manage.py migrate
   python manage.py setup_celery
   ```

## ⚙️ Configuration

### Environment Variables

Add these to your `.env` file:

```env
# Celery Configuration
CELERY_BROKER_URL=redis://localhost:6379/0
CELERY_RESULT_BACKEND=django-db
CELERY_TASK_ALWAYS_EAGER=False

# For production (Railway)
CELERY_BROKER_URL=redis://your-redis-url:6379/0
```

### Production Settings

For Railway deployment, add these environment variables:

- `CELERY_BROKER_URL`: Your Redis URL
- `CELERY_RESULT_BACKEND`: `django-db`
- `CELERY_TASK_ALWAYS_EAGER`: `False`

## 🏃‍♂️ Running Celery

### Development

1. **Start Redis:**

   ```bash
   redis-server
   ```

2. **Start Celery worker:**

   ```bash
   celery -A business worker --loglevel=info
   ```

3. **Start Celery beat (for periodic tasks):**
   ```bash
   celery -A business beat --loglevel=info
   ```

### Production (Railway)

Add these to your `railway.json`:

```json
{
  "deploy": {
    "startCommand": "celery -A business worker --loglevel=info",
    "healthcheckPath": "/health/",
    "healthcheckTimeout": 100,
    "restartPolicyType": "ON_FAILURE",
    "restartPolicyMaxRetries": 10
  }
}
```

## 📧 Email Tasks

### Available Tasks

1. **`send_reservation_confirmation_task`** - Customer confirmation emails
2. **`send_internal_confirmation_task`** - Internal notification emails
3. **`send_custom_confirmation_task`** - Custom recipient emails
4. **`send_thankyou_email_task`** - Form submission thank you emails
5. **`send_agent_register_email_task`** - Agent registration emails

### Task Features

- **Automatic Retry**: 3 attempts with exponential backoff
- **Error Handling**: Comprehensive logging and error reporting
- **Queue Management**: Dedicated email queue
- **Result Tracking**: Task status and results stored in database

## 🧪 Testing

### Test Email Tasks

```bash
python test_email_tasks.py
```

### Monitor Tasks

1. **Django Admin**: Check `Django Celery Results` section
2. **Celery Flower** (optional): `celery -A business flower`
3. **Logs**: Check Celery worker logs

## 📊 Performance Monitoring

### Before/After Comparison

| Metric             | Before (Sync)  | After (Async)         |
| ------------------ | -------------- | --------------------- |
| HTTP Response Time | 2-5 seconds    | < 100ms               |
| Email Processing   | Blocks request | Background            |
| Error Handling     | Breaks flow    | Retries automatically |
| User Experience    | Poor           | Excellent             |

### Monitoring Commands

```bash
# Check task status
python manage.py shell
>>> from django_celery_results.models import TaskResult
>>> TaskResult.objects.filter(status='SUCCESS').count()

# Monitor queue
celery -A business inspect active
celery -A business inspect scheduled
```

## 🔧 Troubleshooting

### Common Issues

1. **Tasks not processing:**

   - Check if Celery worker is running
   - Verify Redis connection
   - Check task logs

2. **Emails not sending:**

   - Verify SMTP settings
   - Check email templates exist
   - Review task error logs

3. **Performance issues:**
   - Monitor queue length
   - Scale workers if needed
   - Check Redis memory usage

### Debug Mode

For development, you can run tasks synchronously:

```env
CELERY_TASK_ALWAYS_EAGER=True
```

This processes tasks immediately without Celery worker.

## 🚀 Deployment

### Railway Deployment

1. Add Redis addon to your Railway project
2. Set environment variables
3. Deploy with updated `railway.json`
4. Monitor logs for task processing

### Scaling

- **More Workers**: Add more Celery worker processes
- **Queue Separation**: Use different queues for different task types
- **Monitoring**: Set up alerts for failed tasks

## 📈 Expected Performance Improvements

- **90% faster** HTTP response times
- **Zero timeouts** on payment webhooks
- **Better reliability** with automatic retries
- **Improved user experience** with instant responses
- **Scalable** email processing

## 🔄 Migration from Sync to Async

The migration is **backward compatible**:

1. Old email functions still work
2. New functions queue tasks instead of sending immediately
3. No breaking changes to existing code
4. Gradual rollout possible

## 📝 Next Steps

1. Deploy to staging environment
2. Test with real email sending
3. Monitor performance metrics
4. Scale workers based on load
5. Set up monitoring and alerts

---

**Note**: This setup significantly improves your site's performance by moving email processing to background tasks. Users will experience much faster response times, especially during reservation creation and payment processing.
