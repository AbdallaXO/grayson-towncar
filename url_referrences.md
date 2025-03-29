# Django URL Reference for Frontend Developers

This reference shows how to use Django's `{% url %}` tags along with the actual route paths so you can link pages cleanly in templates.

---

## 🌐 URL Overview

| Page            | URL Path                  | Django URL Tag Usage                         |
|-----------------|---------------------------|----------------------------------------------|
| Home            | `/`                       | `{% url 'home' %}`                            |
| Login           | `/users/login/`           | `{% url 'login' %}`                           |
| Logout          | `/users/logout/`          | `{% url 'logout' %}`                          |
| Services        | `/services/`              | `{% url 'services' %}`                        |
| Rates           | `/rates/`                 | `{% url 'rates' %}`                           |
| FAQs            | `/faqs/`                  | `{% url 'faqs' %}`                            |
| Book a Ride     | `/book/{queryparameter}`  | `{% url 'book-ride' %}`                      |
| About Us        | `/about/`                 | `{% url 'about-us' %}`                       |
| Blog List       | `/blog/`                  | `{% url 'blog-list' %}`                      |
| Blog Post       | `/blog/post/<slug>/`      | `{% url 'blog-post' slug=post.slug %}`       |

---

## 🧩 Template Examples

### Main Navigation
```html
<a href="{% url 'home' %}">Home</a>
<a href="{% url 'services' %}">Services</a>
<a href="{% url 'rates' %}">Rates</a>
<a href="{% url 'faqs' %}">FAQs</a>
<a href="{% url 'book-ride' %}">Book Now</a>
<a href="{% url 'about-us' %}">About</a>
<a href="{% url 'blog-list' %}">Blog</a>
```

### Blog Post Loop Example
```django
{% for post in posts %}
  <a href="{% url 'blog-post' slug=post.slug %}">{{ post.title }}</a>
{% endfor %}
```

### Authentication Buttons
```html
<a href="{% url 'login' %}">Login</a>
<a href="{% url 'logout' %}">Logout</a>
```
